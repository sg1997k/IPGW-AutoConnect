#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东北大学校园网 (ipgw.neu.edu.cn) 自动认证核心
==============================================

只做三件事: 查状态、登录、登出。不带任何界面, 可单独使用。
认证链路(实测有效):

  1. GET  pass.neu.edu.cn/tpass/login?service=...   取 lt / execution / RSA 公钥
  2. POST 同一地址, 表单只发 rsa(=RSA(学号+密码)) / ul / pl / lt / execution / _eventId
     跟随跳转, 从最终 URL 的 ticket= 取票据
  3. GET  ipgw.neu.edu.cn/v1/srun_portal_sso?ac_id=X&ticket=ST-xxx   ← 真正建立上网会话
     (注意是 /v1/ 这个 JSON 接口, 不是网页路径 /srun_portal_sso)

参考实现: Neboer/ipgw-py-manager (ipgw/core/api/SSO.py, portal.py)
"""

import re
import json
import time
import base64
import random
import socket
import struct
import urllib.parse
import urllib.request
import urllib.error
import http.cookiejar
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Optional, Tuple

PORTAL = "ipgw.neu.edu.cn"
SSO = "https://pass.neu.edu.cn"
SERVICE = "http://ipgw.neu.edu.cn/srun_portal_sso?ac_id=1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 12

# 东大统一身份认证登录页写死的 RSA 公钥(公开信息, 随时可从 login_neu_new.js 读到)
RSA_PUBLIC_KEY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnjA28DLKXZzxbKmo9/1WkVLf1mr"
    "+wtLXLXt6sC4WiBCtsbzF5ewm7ARZeAdS3iZtqlYPn6IcUoOw42H8nAK/tfFcIb6dZ1K0atn"
    "0U39oWCGPzYuKtLJeMuNZiDXVuAXtojrckOjLW9B3gUnaNGLuIx0fYe66l0o9WjU2cGLNZQf"
    "iIxs2h00z1EA9IdSnVxiVQWSD+lsP3JZXh2TT287la4Y4603SQNKTK/QvXfcmccwTEd1IW6H"
    "wGxD6QrkInBiHisKWxmveN7UDSaQRZ/J97G0YC32pD38WT53izXeK0p/kU/X37VP555um1w"
    "VWFvPIuc9I7gMP1+hq5a+X6c++tQIDAQAB"
)


# ------------------------------------------------------------------ 网络
def local_ip() -> Optional[str]:
    for probe in (("202.118.8.8", 80), ("202.118.21.1", 80), ("114.114.114.114", 80)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(2)
            s.connect(probe)
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith(("127.", "100.")):
                return ip
        except Exception:
            continue
    try:
        ip = socket.gethostbyname(socket.gethostname())
        return ip if not ip.startswith("127.") else None
    except Exception:
        return None


def _make_opener():
    """构造 opener。

    必须显式禁用代理: 校园网认证是走内网直连的, 一旦系统里开着 Clash/V2Ray
    这类全局代理(会写 HTTP_PROXY 环境变量或系统代理), 请求就会被转给代理,
    代理一退出端口就死, 表现就是"认证页打不开"。所以这里强制直连。
    """
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),          # 空字典 = 不走任何代理
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=_ssl_ctx()),
    )
    return op


def _ssl_ctx():
    import ssl
    return ssl._create_unverified_context()


def _get(opener, url, referer=None, timeout=TIMEOUT) -> str:
    h = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    with opener.open(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _jsonp(text: str) -> dict:
    text = text.strip()
    a, b = text.find("("), text.rfind(")")
    if a < 0 or b < 0:
        raise ValueError("非 JSONP 响应")
    return json.loads(text[a + 1:b])


# ------------------------------------------------------------------ RSA
def _der_len(b, i):
    l = b[i]
    i += 1
    if l & 0x80:
        n = l & 0x7F
        l = int.from_bytes(b[i:i + n], "big")
        i += n
    return l, i


def _parse_pubkey(b64str):
    der = base64.b64decode(b64str)
    _, i = _der_len(der, 1)
    if der[i] == 0x30:
        alg_len, j = _der_len(der, i + 1)
        i = j + alg_len
    bs_len, j = _der_len(der, i + 1)
    inner = der[j + 1:j + bs_len]
    _, k = _der_len(inner, 1)
    n_len, k = _der_len(inner, k + 1)
    n = int.from_bytes(inner[k:k + n_len], "big")
    k += n_len
    e_len, k = _der_len(inner, k + 1)
    e = int.from_bytes(inner[k:k + e_len], "big")
    return n, e


def rsa_encrypt(msg: bytes, n, e) -> str:
    k = (n.bit_length() + 7) // 8
    pad = k - len(msg) - 3
    if pad < 8:
        raise ValueError("待加密内容过长")
    ps = bytes(random.randint(1, 255) for _ in range(pad))
    em = b"\x00\x02" + ps + b"\x00" + msg
    return base64.b64encode(pow(int.from_bytes(em, "big"), e, n).to_bytes(k, "big")).decode()


# ------------------------------------------------------------------ 对外接口
def query_status(username: str = None) -> dict:
    """保留 online/error/info，并区分网关明确下线与查询失败。"""
    try:
        text = _get(_make_opener(), "http://%s/cgi-bin/rad_user_info?callback=cb&_=%d"
                    % (PORTAL, int(time.time() * 1000)))
        payload = re.fullmatch(r"cb\((.*)\)\s*;?", text.strip(), re.DOTALL)
        data = json.loads(payload.group(1) if payload else text)
        if not isinstance(data, dict):
            raise ValueError("状态响应格式无效")
    except (OSError, HTTPException, ValueError) as ex:
        return {"state": "unknown", "online": False, "info": None,
                "error": "校园网状态接口未返回有效响应（%s）" % type(ex).__name__}

    error = data.get("error")
    if error == "ok" and data.get("user_name"):
        if username and str(data["user_name"]) != str(username):
            state, message = "other", "当前校园网会话属于其他账号"
        else:
            state, message = "online", ""
    elif error == "not_online_error":
        state, message = "offline", "校园网网关报告当前连接未认证"
    else:
        state, message = "unknown", "校园网状态接口未确认会话"
    return {"state": state, "online": state == "online", "error": message, "info": data}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def internet_available():
    """通过系统网络路径验证 HTTPS 204；不携带校园网凭据或 Cookie。"""
    url = "https://connectivitycheck.platform.hicloud.com/generate_204"
    try:
        with urllib.request.build_opener(_NoRedirect()).open(url, timeout=4) as reply:
            return reply.status == 204 and not reply.read(1)
    except (OSError, HTTPException, ValueError):
        return False


@contextmanager
def _authentication_slot():
    # GUI 和 SYSTEM 可同时运行。只在实际认证及确认期间占用本机端口。
    with socket.socket() as guard:
        guard.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            guard.bind(("127.0.0.1", 47323))
        except OSError:
            yield False
        else:
            yield True



def login(username: str, password: str, ac_id: int = 1) -> Tuple[bool, str]:
    """执行统一认证登录。返回 (成功?, 说明)。"""
    if not username or not password:
        return False, "账号或密码为空"
    n, e = _parse_pubkey(RSA_PUBLIC_KEY)
    opener = _make_opener()
    login_url = "%s/tpass/login?service=%s" % (SSO, urllib.parse.quote(SERVICE, safe=""))

    # 1. 登录页
    try:
        html = _get(opener, login_url)
    except Exception as ex:
        return False, "认证页打不开(可能没网): %s" % ex

    m_lt = re.search(r'name="lt"\s+value="([^"]+)"', html)
    if not m_lt:
        return False, "登录页结构变了, 未取到 lt"
    lt = m_lt.group(1)
    m_ex = re.search(r'name="execution"\s+value="([^"]+)"', html)
    execution = m_ex.group(1) if m_ex else "e1s1"

    # 2. 提交(只发 rsa, 不发明文密码)
    body = urllib.parse.urlencode({
        "rsa": rsa_encrypt((username + password).encode("utf-8"), n, e),
        "ul": str(len(username)),
        "pl": str(len(password)),
        "lt": lt,
        "execution": execution,
        "_eventId": "submit",
    }).encode()
    hdr = {
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": login_url,
        "Origin": SSO,
    }
    try:
        r = opener.open(urllib.request.Request(login_url, data=body, headers=hdr), timeout=25)
        final_url = r.geturl()
        page = r.read().decode("utf-8", "replace")
    except Exception as ex:
        return False, "认证提交失败: %s" % ex

    m_ticket = re.search(r"ticket=([^&\s]+)", final_url or "")
    if not m_ticket and "智慧东大" in page and "统一身份认证" not in page:
        # 邮箱验证页, 再跳一次
        try:
            r2 = opener.open(urllib.request.Request(login_url, headers={"User-Agent": UA}), timeout=25)
            m_ticket = re.search(r"ticket=([^&\s]+)", r2.geturl() or "")
        except Exception:
            pass
    if not m_ticket:
        # 只认明确的错误文案, 免得抓到页面上无关的词(比如"密码面板")误导用户
        hint = re.search(
            r"(密码错误|密码不正确|账号或密码错误|验证码错误|账号已被锁定|账号或密码不正确|用户不存在|无效的用户名)",
            page)
        if hint:
            return False, "认证未通过: %s" % hint.group(1)
        return False, "认证未通过(请检查账号密码是否正确; 也可能是账号需要扫码/邮箱验证)"

    ticket = m_ticket.group(1)

    # 3. 建连 —— 必须是 /v1/ 这个 JSON 接口
    api_url = "http://%s/v1/srun_portal_sso?ac_id=%s&ticket=%s" % (PORTAL, ac_id, ticket)
    try:
        resp = _get(opener, api_url, referer=api_url, timeout=15)
    except Exception as ex:
        return False, "建连接口失败: %s" % ex

    # 接口返回 {"code":0,...} 成功; {"code":1,"message":"ip_already_online_error"} 已在线
    try:
        data = json.loads(resp)
        code = data.get("code")
        msg = str(data.get("message") or "")
        if code in (0, "0"):
            return True, "登录成功"
        if "already_online" in msg:
            return True, "已经在线"
        return False, "建连被拒: %s" % (msg or resp[:80])
    except Exception:
        return True, "已提交认证"

    # 4. 复查(一般 code=0 已生效, 这里兜底确认一下)


def confirm_online(username: str, wait_sec: int = 20) -> Tuple[bool, str]:
    """登录后轮询确认, 避免服务端异步登记带来的误判。"""
    status = _wait_for_session(username, wait_sec)
    return status["online"], "已确认在线" if status["online"] else "认证已提交，等待网关确认"


def _wait_for_session(username, wait_sec):
    deadline = time.monotonic() + wait_sec
    while True:
        status = query_status(username)
        remaining = deadline - time.monotonic()
        if status["state"] != "offline" or remaining <= 0:
            return status
        time.sleep(min(3, remaining))



def logout(ac_id: int = 1) -> dict:
    """注销本机当前上网会话。"""
    ip = local_ip()
    if not ip:
        return {"error": "no_ip"}
    opener = _make_opener()
    try:
        text = _get(opener, "http://%s/cgi-bin/srun_portal?%s" % (
            PORTAL, urllib.parse.urlencode({
                "callback": "cb", "action": "logout",
                "ip": ip, "ac_id": str(ac_id), "_": int(time.time() * 1000)})))
        return _jsonp(text)
    except Exception as ex:
        return {"error": "request_failed", "detail": repr(ex)}


@dataclass
class Connection:
    state: str
    message: str
    info: dict = field(default_factory=dict)
    restored: bool = False


def check_connection(username, password, ac_id=1, wait_sec=20):
    def observed(status):
        if status["online"]:
            return Connection("online", "校园网已认证", status["info"])
        return Connection(status["state"], status["error"])

    status = query_status(username)
    if status["state"] in ("online", "other"):
        return observed(status)
    if internet_available():
        return Connection("reachable", "互联网可用，校园网认证状态待确认")
    if status["state"] != "offline":
        return observed(status)

    with _authentication_slot() as acquired:
        if not acquired:
            return Connection("unknown", "等待其他实例完成校园网认证")
        status = query_status(username)
        if status["state"] != "offline":
            return observed(status)
        ok, message = login(username, password, ac_id)
        if not ok:
            return Connection("failed", message)
        status = _wait_for_session(username, wait_sec)
        if status["online"]:
            return Connection("online", "校园网认证已恢复", status["info"], restored=True)
        return Connection("pending", "认证已提交，等待网关确认")


def ensure_online(username: str, password: str, ac_id: int = 1,
                  wait_sec: int = 20) -> Tuple[bool, str]:
    """兼容原有调用，只有确认此账号已认证才返回 True。"""
    result = check_connection(username, password, ac_id, wait_sec)
    return result.state == "online", result.message
