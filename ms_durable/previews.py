"""Bounded Open Graph lookup over HTTPS, without cookies or ambient proxies."""

import http.client
import ipaddress
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .media import RASTER


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        attrs = dict(attrs)
        key = attrs.get("property")
        value = attrs.get("content")
        if key in {"og:title", "og:description", "og:site_name", "og:image"} and value:
            self.values.setdefault(
                key[3:], " ".join(value.split())[: 2048 if key == "og:image" else 400]
            )


def fetch(url, domains, any_domain, accepted, limit):
    """Check every redirect and connect only to an address checked for that hop."""
    for _ in range(3):
        target = urlsplit(url)
        host = target.hostname
        if (
            len(url) > 2048
            or target.scheme != "https"
            or not host
            or target.username is not None
            or target.password is not None
            or target.port not in (None, 443)
        ):
            raise ValueError("Only public HTTPS links are supported")
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if not any_domain and host not in domains:
            raise ValueError("Domain not enabled for previews")
        ips = {info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        if not ips or any(
            not ipaddress.ip_address(ip).is_global or ipaddress.ip_address(ip).is_multicast
            for ip in ips
        ):
            raise ValueError("Preview destination is not public")
        conn = http.client.HTTPSConnection(host, timeout=3)
        # Preserve hostname verification and SNI without resolving the host again.
        address = min(ips)

        def connect(_address, timeout, source_address, address=address):
            return socket.create_connection((address, 443), timeout, source_address)

        conn._create_connection = connect
        try:
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            conn.request(
                "GET",
                path,
                headers={"Accept": ", ".join(accepted), "User-Agent": "MS-Durable link preview"},
            )
            response = conn.getresponse()
            if response.status in {301, 302, 303, 307, 308} and response.getheader("Location"):
                url = urljoin(url, response.getheader("Location"))
                continue
            mime = response.getheader("Content-Type", "").split(";")[0].strip().lower()
            if response.status == 200 and mime in accepted:
                return url, mime, response.read(limit)
            return None
        finally:
            conn.close()
    return None


def preview(url, domains, any_domain):
    result = fetch(url, domains, any_domain, ("text/html",), 128 * 1024)
    if not result:
        return {}
    url, _, content = result
    parser = Metadata()
    parser.feed(content.decode("utf-8", errors="replace"))
    if parser.values.get("image"):
        parser.values["image"] = urljoin(url, parser.values["image"])
    return parser.values


def thumbnail(url, domains, any_domain):
    limit = 512 * 1024
    result = fetch(url, domains, any_domain, RASTER, limit + 1)
    if result:
        _, mime, content = result
        if len(content) <= limit and RASTER[mime](content):
            return mime, content
    return None
