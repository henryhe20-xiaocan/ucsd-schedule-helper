"""Open only a verified current instance, not an unrelated service on this port."""
import json
import socket
import urllib.request
import webbrowser


def main():
    url = 'http://127.0.0.1:8778/'
    with socket.socket() as sock:
        occupied = sock.connect_ex(('127.0.0.1', 8778)) == 0
    if occupied:
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url + 'api/health', timeout=3) as response:
                status = json.load(response)
            if status.get('app') == 'ucsd-scheduler' and status.get('version') == 2:
                webbrowser.open(url)
                return
        except Exception:
            pass
        print('8778 已被旧版排课助手或其他程序占用。请先关闭旧服务窗口，再重新启动。')
        raise SystemExit(1)
    from scheduler import main as run
    run()


if __name__ == '__main__':
    main()
