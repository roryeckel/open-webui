import asyncio
from datetime import datetime, time, timedelta
import socket
import ssl
import urllib.parse
import certifi
import validators
from collections import defaultdict
from typing import AsyncIterator, Dict, List, Optional, Union, Sequence, Iterator

from langchain_community.document_loaders import (
    WebBaseLoader,
    PlaywrightURLLoader
)
from langchain_core.documents import Document


from open_webui.constants import ERROR_MESSAGES
from open_webui.config import ENABLE_RAG_LOCAL_WEB_FETCH, PLAYWRIGHT_WS_URI, RAG_WEB_LOADER
from open_webui.env import SRC_LOG_LEVELS

import logging

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["RAG"])


def validate_url(url: Union[str, Sequence[str]]):
    if isinstance(url, str):
        if isinstance(validators.url(url), validators.ValidationError):
            raise ValueError(ERROR_MESSAGES.INVALID_URL)
        if not ENABLE_RAG_LOCAL_WEB_FETCH:
            # Local web fetch is disabled, filter out any URLs that resolve to private IP addresses
            parsed_url = urllib.parse.urlparse(url)
            # Get IPv4 and IPv6 addresses
            ipv4_addresses, ipv6_addresses = resolve_hostname(parsed_url.hostname)
            # Check if any of the resolved addresses are private
            # This is technically still vulnerable to DNS rebinding attacks, as we don't control WebBaseLoader
            for ip in ipv4_addresses:
                if validators.ipv4(ip, private=True):
                    raise ValueError(ERROR_MESSAGES.INVALID_URL)
            for ip in ipv6_addresses:
                if validators.ipv6(ip, private=True):
                    raise ValueError(ERROR_MESSAGES.INVALID_URL)
        return True
    elif isinstance(url, Sequence):
        return all(validate_url(u) for u in url)
    else:
        return False

def safe_validate_urls(url: Sequence[str]) -> Sequence[str]:
    valid_urls = []
    for u in url:
        try:
            if validate_url(u):
                valid_urls.append(u)
        except ValueError:
            continue
    return valid_urls

def resolve_hostname(hostname):
    # Get address information
    addr_info = socket.getaddrinfo(hostname, None)

    # Extract IP addresses from address information
    ipv4_addresses = [info[4][0] for info in addr_info if info[0] == socket.AF_INET]
    ipv6_addresses = [info[4][0] for info in addr_info if info[0] == socket.AF_INET6]

    return ipv4_addresses, ipv6_addresses

def extract_metadata(soup, url):
    metadata = {
        "source": url
    }
    if title := soup.find("title"):
        metadata["title"] = title.get_text()
    if description := soup.find("meta", attrs={"name": "description"}):
        metadata["description"] = description.get(
            "content", "No description found."
        )
    if html := soup.find("html"):
        metadata["language"] = html.get("lang", "No language found.")
    return metadata

class SafePlaywrightURLLoader(PlaywrightURLLoader):
    """Load HTML pages safely with Playwright, supporting SSL verification, rate limiting, and remote browser connection.
    
    Attributes:
        urls (List[str]): List of URLs to load.
        verify_ssl (bool): If True, verify SSL certificates.
        requests_per_second (Optional[float]): Number of requests per second to limit to.
        continue_on_failure (bool): If True, continue loading other URLs on failure.
        headless (bool): If True, the browser will run in headless mode.
        playwright_ws_url (Optional[str]): WebSocket endpoint URI for remote browser connection.
    """

    def __init__(
        self,
        urls: List[str],
        verify_ssl: bool = True,
        requests_per_second: Optional[float] = None,
        continue_on_failure: bool = True,
        headless: bool = True,
        remove_selectors: Optional[List[str]] = None,
        proxy: Optional[Dict[str, str]] = None,
        playwright_ws_url: Optional[str] = None
    ):
        """Initialize with additional safety parameters and remote browser support."""
        # We'll set headless to False if using playwright_ws_url since it's handled by the remote browser
        super().__init__(
            urls=urls,
            continue_on_failure=continue_on_failure,
            headless=headless if playwright_ws_url is None else False,
            remove_selectors=remove_selectors,
            proxy=proxy
        )
        self.verify_ssl = verify_ssl
        self.requests_per_second = requests_per_second
        self.last_request_time = None
        self.playwright_ws_url = playwright_ws_url

    def lazy_load(self) -> Iterator[Document]:
        """Safely load URLs synchronously with support for remote browser."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            # Use remote browser if ws_endpoint is provided, otherwise use local browser
            if self.playwright_ws_url:
                browser = p.chromium.connect(self.playwright_ws_url)
            else:
                browser = p.chromium.launch(headless=self.headless, proxy=self.proxy)

            for url in self.urls:
                try:
                    self._safe_process_url_sync(url)
                    page = browser.new_page()
                    response = page.goto(url)
                    if response is None:
                        raise ValueError(f"page.goto() returned None for url {url}")

                    text = self.evaluator.evaluate(page, browser, response)
                    metadata = {"source": url}
                    yield Document(page_content=text, metadata=metadata)
                except Exception as e:
                    if self.continue_on_failure:
                        log.exception(e, "Error loading %s", url)
                        continue
                    raise e
            browser.close()

    async def alazy_load(self) -> AsyncIterator[Document]:
        """Safely load URLs asynchronously with support for remote browser."""
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            # Use remote browser if ws_endpoint is provided, otherwise use local browser
            if self.playwright_ws_url:
                browser = await p.chromium.connect(self.playwright_ws_url)
            else:
                browser = await p.chromium.launch(headless=self.headless, proxy=self.proxy)

            for url in self.urls:
                try:
                    await self._safe_process_url(url)
                    page = await browser.new_page()
                    response = await page.goto(url)
                    if response is None:
                        raise ValueError(f"page.goto() returned None for url {url}")

                    text = await self.evaluator.evaluate_async(page, browser, response)
                    metadata = {"source": url}
                    yield Document(page_content=text, metadata=metadata)
                except Exception as e:
                    if self.continue_on_failure:
                        log.exception(e, "Error loading %s", url)
                        continue
                    raise e
            await browser.close()

    def _verify_ssl_cert(self, url: str) -> bool:
        """Verify SSL certificate for the given URL."""
        if not url.startswith("https://"):
            return True
            
        try:
            hostname = url.split("://")[-1].split("/")[0]
            context = ssl.create_default_context(cafile=certifi.where())
            with context.wrap_socket(ssl.socket(), server_hostname=hostname) as s:
                s.connect((hostname, 443))
            return True
        except ssl.SSLError:
            return False
        except Exception as e:
            log.warning(f"SSL verification failed for {url}: {str(e)}")
            return False

    async def _wait_for_rate_limit(self):
        """Wait to respect the rate limit if specified."""
        if self.requests_per_second and self.last_request_time:
            min_interval = timedelta(seconds=1.0 / self.requests_per_second)
            time_since_last = datetime.now() - self.last_request_time
            if time_since_last < min_interval:
                await asyncio.sleep((min_interval - time_since_last).total_seconds())
        self.last_request_time = datetime.now()

    def _sync_wait_for_rate_limit(self):
        """Synchronous version of rate limit wait."""
        if self.requests_per_second and self.last_request_time:
            min_interval = timedelta(seconds=1.0 / self.requests_per_second)
            time_since_last = datetime.now() - self.last_request_time
            if time_since_last < min_interval:
                time.sleep((min_interval - time_since_last).total_seconds())
        self.last_request_time = datetime.now()

    async def _safe_process_url(self, url: str) -> bool:
        """Perform safety checks before processing a URL."""
        if self.verify_ssl and not self._verify_ssl_cert(url):
            raise ValueError(f"SSL certificate verification failed for {url}")
        await self._wait_for_rate_limit()
        return True

    def _safe_process_url_sync(self, url: str) -> bool:
        """Synchronous version of safety checks."""
        if self.verify_ssl and not self._verify_ssl_cert(url):
            raise ValueError(f"SSL certificate verification failed for {url}")
        self._sync_wait_for_rate_limit()
        return True

class SafeWebBaseLoader(WebBaseLoader):
    """WebBaseLoader with enhanced error handling for URLs."""

    def lazy_load(self) -> Iterator[Document]:
        """Lazy load text from the url(s) in web_path with error handling."""
        for path in self.web_paths:
            try:
                soup = self._scrape(path, bs_kwargs=self.bs_kwargs)
                text = soup.get_text(**self.bs_get_text_kwargs)

                # Build metadata
                metadata = extract_metadata(soup, path)

                yield Document(page_content=text, metadata=metadata)
            except Exception as e:
                # Log the error and continue with the next URL
                log.exception(e, "Error loading %s", path)

RAG_WEB_LOADERS = defaultdict(lambda: SafeWebBaseLoader)
RAG_WEB_LOADERS["playwright"] = SafePlaywrightURLLoader
RAG_WEB_LOADERS["safe_web"] = SafeWebBaseLoader

def get_web_loader(
    urls: Union[str, Sequence[str]],
    verify_ssl: bool = True,
    requests_per_second: int = 2,
):
    # Check if the URLs are valid
    safe_urls = safe_validate_urls([urls] if isinstance(urls, str) else urls)

    web_loader_args = {
        "urls": safe_urls,
        "verify_ssl": verify_ssl,
        "requests_per_second": requests_per_second,
        "continue_on_failure": True
    }

    if PLAYWRIGHT_WS_URI.value:
        web_loader_args["playwright_ws_url"] = PLAYWRIGHT_WS_URI.value

    # Create the appropriate WebLoader based on the configuration
    WebLoaderClass = RAG_WEB_LOADERS[RAG_WEB_LOADER.value]
    web_loader = WebLoaderClass(**web_loader_args)

    log.debug("Using RAG_WEB_LOADER %s for %s URLs", web_loader.__class__.__name__, len(safe_urls))

    return web_loader