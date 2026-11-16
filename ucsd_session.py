"""UCSD session owner. All Playwright objects live on one asyncio loop.

Login is interactive. Only the three read-only catalogue operations are exposed.
Authentication state is local user data, never part of saved_data.json.
"""
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import threading
import time
import urllib.parse
from private_state import unprotect

BASE = "https://classplanner.apps.ucsd.edu"
LOGIN_PATH = "/start"
ALLOWED = {
    ("GET", "/auth/session"),
    ("GET", "/api/v1/planner/terms"),
    ("GET", "/api/v1/planner/courses"),
    ("POST", "/api/v1/catalog/courses/search"),
}


class SessionError(RuntimeError):
    def __init__(self, message, code="UCSD_ERROR", status=502):
        super().__init__(message)
        self.code, self.status = code, status


class UCSDSession:
    def __init__(self, state_path=None):
        root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "UCSD-Schedule-Helper"
        self.state_path = Path(state_path) if state_path else root / "auth-state.dpapi"
        self.profile_path = self.state_path.parent.resolve() / "browser-profile"
        self._lock = threading.Lock()
        self._status = {"state": "signed_out", "message": "请点击登录 UCSD"}
        self._thread = None
        self._loop = None
        self._ready = threading.Event()
        self._context = self._pw = self._poll = None
        self._headless = False
        self._background = False
        self._csrf_token = ""

    def status(self):
        with self._lock:
            return dict(self._status)

    def _set(self, state, message):
        with self._lock:
            self._status = {"state": state, "message": message}

    def _owner(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._serial = asyncio.Lock()
        self._ready.set()
        loop.run_forever()
        loop.close()

    def _submit(self, factory):
        with self._lock:
            if not self._thread:
                self._thread = threading.Thread(target=self._owner, daemon=True)
                self._thread.start()
        if not self._ready.wait(5):
            raise SessionError("登录服务未能启动")
        return asyncio.run_coroutine_threadsafe(self._serialized(factory), self._loop)

    async def _serialized(self, factory):
        async with self._serial:
            return await factory()

    def login(self):
        # Set before queuing: double clicks cannot launch multiple browsers.
        with self._lock:
            if self._status["state"] in {"signing_in", "recovering"}:
                return dict(self._status)
            self._status = {"state": "signing_in", "message": "请在弹出的专用浏览器完成 UCSD / Duo 登录"}
        self._submit(self._login)
        return self.status()

    async def _close_context(self):
        context = self._context
        self._context = None
        if context:
            await context.close()

    def _profile_directory(self):
        # The only directory this app can open or remove as its browser profile.
        path = self.profile_path
        if path != self.state_path.parent.resolve() / "browser-profile" or path.resolve() != path:
            raise SessionError("专用浏览器配置路径无效")
        return path

    def _on_context_closed(self, context):
        if self._context is context:
            self._context = None
            self._csrf_token = ""
            self._set("expired", "专用浏览器已关闭，点击登录 UCSD 可恢复")

    async def _launch_context(self, *, background=False):
        from playwright.async_api import Error, async_playwright
        profile = self._profile_directory()
        first_launch = not profile.exists()
        if not self._pw:
            self._pw = await async_playwright().start()
        context = await self._pw.chromium.launch_persistent_context(
            profile, channel="chromium", headless=background or self._headless, timeout=30000,
            # Allow Chromium's own save-password prompt and native credential store.
            ignore_default_args=["--enable-automation", "--password-store=basic", "--use-mock-keychain"],
        )
        self._context = context
        self._background = background
        context.on("close", lambda: self._on_context_closed(context))
        if first_launch and self.state_path.is_file():
            try:
                saved = json.loads(unprotect(self.state_path.read_bytes()))
                now = time.time()
                cookies = [cookie for cookie in saved["cookies"]
                           if cookie["expires"] == -1 or cookie["expires"] > now]
                if cookies:
                    await context.add_cookies(cookies)
            except (Error, OSError, ValueError, TypeError, KeyError):
                # An unusable legacy snapshot should not prevent manual sign-in.
                pass

    async def _switch_context(self, *, background):
        # Chromium may drop session-only cookies when it exits. Transfer those
        # in memory while reopening the same profile in the other mode.
        cookies = [cookie for cookie in await self._context.cookies()
                   if cookie["expires"] == -1]
        await self._close_context()
        await self._launch_context(background=background)
        if cookies:
            await self._context.add_cookies(cookies)

    async def _login(self):
        try:
            if not self._context and self._profile_directory().exists():
                await self._launch_context(background=True)
            if self._context and self._background:
                try:
                    await self._probe(timeout=5)
                    self._set("authenticated", "UCSD 已连接（后台）")
                    return
                except SessionError as exc:
                    if exc.code != "AUTH_REQUIRED":
                        raise
                await self._switch_context(background=False)
            if self._context:
                try:
                    pages = self._context.pages
                    page = pages[0] if pages else await self._context.new_page()
                    await page.bring_to_front()
                except Exception:
                    await self._close_context()
            if not self._context:
                await self._launch_context()
                pages = self._context.pages
                page = pages[0] if pages else await self._context.new_page()
            await page.goto(BASE + LOGIN_PATH, wait_until="domcontentloaded", timeout=60000)
            self._poll = asyncio.create_task(self._wait_for_login())
        except Exception as exc:
            detail = str(exc).lower()
            if isinstance(exc, ModuleNotFoundError) or "executable doesn't exist" in detail:
                message = "登录组件未安装完整，请运行安装登录组件.bat"
            elif "cert" in detail:
                message = "UCSD 登录页面的证书校验失败，请修复网络或证书配置后重试"
            elif "processsingleton" in detail or "profile appears to be in use" in detail:
                message = "专用浏览器配置正在使用，请关闭另一排课助手窗口后重试"
            else:
                message = "无法打开 UCSD 登录页面，请检查网络或关闭专用登录窗口后重试"
            self._set("error", message)

    async def _wait_for_login(self):
        deadline = time.monotonic() + 600
        failures = 0
        while self._context and time.monotonic() < deadline:
            try:
                await self._serialized(self._finish_login)
                return
            except SessionError as exc:
                if exc.code == "UCSD_ERROR":
                    failures += 1
                    if failures >= 3:
                        self._set("error", "连续无法访问 UCSD API，请检查网络及证书配置后重试登录")
                        return
                else:
                    failures = 0
                if exc.code not in {"AUTH_REQUIRED", "UCSD_ERROR"}:
                    self._set("error", str(exc))
                    return
            except Exception:
                self._set("error", "登录窗口已关闭或会话保存失败，请重试登录")
                return
            await asyncio.sleep(3)
        if self._context:
            self._set("expired", "登录等待已超时，请重试")

    async def _probe(self, timeout=10):
        await self._refresh_csrf(timeout)
        data = await self._fetch("GET", "/api/v1/planner/terms", None, timeout)
        if not isinstance(data.get("terms"), list):
            raise SessionError("学期接口返回格式不兼容", "SCHEMA_CHANGED")
        if not self._context:
            raise SessionError("专用浏览器已关闭", "AUTH_REQUIRED", 401)
        # Chromium now persists the profile. Retire the legacy snapshot only
        # after the new browser has successfully authenticated.
        self.state_path.unlink(missing_ok=True)

    async def _finish_login(self):
        await self._probe()
        if not self._background:
            await self._switch_context(background=True)
            try:
                await self._probe()
            except SessionError as exc:
                if exc.code == "AUTH_REQUIRED":
                    self._set("expired", "后台会话未通过 UCSD 验证，请点击登录 UCSD 重试")
                    return
                raise
        self._set("authenticated", "UCSD 已连接（后台）")

    async def _refresh_csrf(self, timeout=10):
        # Same contract as the official frontend. Keep the token only in memory.
        data = await self._fetch("GET", "/auth/session", None, timeout)
        token = data.get("csrf_token")
        if not isinstance(token, str) or not token:
            raise SessionError("UCSD 登录验证信息不完整，请重新登录", "AUTH_REQUIRED", 401)
        self._csrf_token = token

    async def _fetch(self, method, path, payload, timeout, retry_csrf=True):
        parsed = urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or (method, parsed.path) not in ALLOWED:
            raise SessionError("不允许的 UCSD 操作", "FORBIDDEN", 403)
        if not self._context:
            raise SessionError("请先点击登录 UCSD", "AUTH_REQUIRED", 401)
        headers = {"Accept": "application/json", "Origin": BASE, "Referer": BASE + "/"}
        if method == "POST":
            if not self._csrf_token:
                await self._refresh_csrf()
            headers["X-CSRF-Token"] = self._csrf_token
        try:
            response = await self._context.request.fetch(
                BASE + path, method=method, data=payload,
                headers=headers,
                timeout=timeout * 1000, max_redirects=0,
            )
        except Exception:
            raise SessionError("UCSD 请求失败，请检查网络或重新登录") from None
        try:
            login_url = urllib.parse.urlsplit(urllib.parse.urljoin(BASE, response.headers.get("location", "")))
            redirects_to_login = (300 <= response.status < 400 and
                                  login_url.netloc == urllib.parse.urlsplit(BASE).netloc and
                                  login_url.path == "/auth/login")
            if response.status == 401 or redirects_to_login:
                raise SessionError("UCSD 登录已过期，请点击登录 UCSD", "AUTH_REQUIRED", 401)
            if response.status == 403:
                # Only the allowlisted read-only course search can be replayed.
                if method == "POST" and retry_csrf:
                    await response.dispose()
                    response = None
                    await self._refresh_csrf()
                    return await self._fetch(method, path, payload, timeout, retry_csrf=False)
                raise SessionError("UCSD 请求验证失败，请重新登录后重试", "UCSD_FORBIDDEN", 403)
            if not 200 <= response.status < 300:
                raise SessionError(f"UCSD 服务返回 HTTP {response.status}")
            if "application/json" not in response.headers.get("content-type", ""):
                raise SessionError("UCSD 未返回预期的 JSON 数据", "SCHEMA_CHANGED")
            try:
                data = await response.json()
            except Exception:
                raise SessionError("UCSD 返回无效 JSON", "SCHEMA_CHANGED") from None
            if not isinstance(data, dict):
                raise SessionError("UCSD 返回格式不兼容")
            return data
        finally:
            if response is not None:
                await response.dispose()

    async def _recover(self):
        with self._lock:
            if self._status["state"] != "authenticated":
                return
            self._status = {"state": "recovering", "message": "正在通过 UCSD 恢复登录会话…"}
        self._csrf_token = ""
        try:
            # Reuse the isolated browser and its existing school SSO cookies.
            pages = self._context.pages
            page = pages[0] if pages else await self._context.new_page()
            await page.goto(BASE + LOGIN_PATH, wait_until="domcontentloaded", timeout=10000)
            for attempt in range(4):
                try:
                    await self._probe(timeout=4)
                    self._set("authenticated", "UCSD 已连接（后台）" if self._background else "UCSD 已连接")
                    return
                except SessionError as exc:
                    if exc.code != "AUTH_REQUIRED":
                        raise
                    if attempt < 3:
                        await asyncio.sleep(1)
            self._set("signing_in", "请在专用浏览器完成 UCSD / Duo，然后重试刚才的操作")
            await self._login()
        except SessionError:
            raise
        except Exception:
            raise SessionError("无法恢复 UCSD 会话，请点击登录 UCSD 后重试") from None
        finally:
            if self.status()["state"] == "recovering":
                self._set("expired", "无法自动恢复 UCSD 会话，请点击登录 UCSD")

    def _check_ready(self, allow_recovery=False):
        status = self.status()
        if status["state"] == "signing_in":
            raise SessionError(status["message"], "AUTH_PENDING", 409)
        if status["state"] == "recovering" and allow_recovery:
            return
        if status["state"] != "authenticated":
            raise SessionError("请先点击登录 UCSD 并完成验证", "AUTH_REQUIRED", 401)

    async def _request(self, method, path, payload, timeout):
        self._check_ready()
        try:
            return await self._fetch(method, path, payload, timeout)
        except SessionError as exc:
            if exc.code != "AUTH_REQUIRED":
                raise
        await self._recover()
        self._check_ready()
        try:
            return await self._fetch(method, path, payload, timeout)
        except SessionError as exc:
            if exc.code == "AUTH_REQUIRED":
                self._csrf_token = ""
                self._set("expired", str(exc))
            raise

    def request(self, method, path, payload=None, timeout=45):
        self._check_ready(allow_recovery=True)
        future = self._submit(lambda: self._request(method, path, payload, timeout))
        try:
            return future.result(timeout=2 * timeout + 100)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise SessionError("UCSD 请求超时，请重试") from None

    def logout(self):
        self._submit(self._logout).result(timeout=15)
        return self.status()

    async def _logout(self):
        self._csrf_token = ""
        if self._poll:
            self._poll.cancel()
            self._poll = None
        await self._close_context()
        try:
            self.state_path.unlink(missing_ok=True)
            profile = self._profile_directory()
            if profile.exists():
                shutil.rmtree(profile)
        except (OSError, SessionError):
            self._set("error", "无法清除专用浏览器配置，请关闭专用窗口后重试")
            raise SessionError(self.status()["message"]) from None
        self._set("signed_out", "本地 UCSD 会话已清除；官网其他窗口不会被退出")

    def close(self):
        if self._loop and self._loop.is_running():
            async def stop():
                if self._poll:
                    self._poll.cancel()
                await self._close_context()
                if self._pw:
                    await self._pw.stop()
            try:
                self._submit(stop).result(timeout=10)
            finally:
                self._loop.call_soon_threadsafe(self._loop.stop)
