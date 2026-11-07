"""Glovebox in-guest network audit, loaded by every Python process in a code sandbox.

Appends each DNS lookup, TCP connect and HTTP response to /tmp/.gbx_net.jsonl so the console can
show what code tried to reach. Observability only: enforcement happens outside the VM in
Microsandbox's network policy, which nothing in this file can change.
"""
import http.client
import json
import socket
import time

_LOG = "/tmp/.gbx_net.jsonl"


def _log(**rec):
    rec["t"] = round(time.time(), 3)
    try:
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


_getaddrinfo = socket.getaddrinfo


def _audited_getaddrinfo(host, port, *args, **kwargs):
    try:
        result = _getaddrinfo(host, port, *args, **kwargs)
    except OSError as e:
        _log(op="dns", host=str(host), port=port, ok=False, error=str(e)[:160])
        raise
    _log(op="dns", host=str(host), port=port, ok=True)
    return result


_connect = socket.socket.connect


def _audited_connect(self, address):
    host, port = (address[0], address[1]) if isinstance(address, tuple) and len(address) > 1 else (address, None)
    try:
        result = _connect(self, address)
    except OSError as e:
        _log(op="connect", host=str(host), port=port, ok=False, error=str(e)[:160])
        raise
    _log(op="connect", host=str(host), port=port, ok=True)
    return result


_putrequest = http.client.HTTPConnection.putrequest
_getresponse = http.client.HTTPConnection.getresponse


def _audited_putrequest(self, method, url, *args, **kwargs):
    self._gbx_request = f"{method} {url}"[:240]
    return _putrequest(self, method, url, *args, **kwargs)


def _audited_getresponse(self, *args, **kwargs):
    response = _getresponse(self, *args, **kwargs)
    _log(op="http", host=self.host, port=self.port, request=getattr(self, "_gbx_request", ""),
         status=response.status, content_type=response.getheader("content-type", ""))
    return response


socket.getaddrinfo = _audited_getaddrinfo
socket.socket.connect = _audited_connect
http.client.HTTPConnection.putrequest = _audited_putrequest
http.client.HTTPConnection.getresponse = _audited_getresponse
