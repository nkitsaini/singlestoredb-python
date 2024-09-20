import asyncio
import os
import signal
import typing
import urllib.parse

from ._portal import portal
if typing.TYPE_CHECKING:
    from plotly.graph_objs import Figure
    from fastapi import FastAPI
    from psutil import Process


def run_dashboard(
    figure: 'Figure',
    kill_existing_server: bool = True,
    host: str = '0.0.0.0',
    debug: bool = False,
) -> None:
    try:
        import dash
    except ImportError:
        raise ImportError('package dash is required to run dashboards')

    port = portal.app_listen_port
    app_url = portal.app_url

    if port is None or app_url is None:
        raise RuntimeError(
           'Portal not fully initialized. '
           'Is the code running outside SingleStoreDB notebook environment?',
        )

    if kill_existing_server:
        _kill_process_by_port(port)

    base_path = urllib.parse.urlparse(app_url).path

    app = dash.Dash(requests_pathname_prefix=base_path)
    app.layout = dash.html.Div([
        dash.dcc.Graph(figure=figure),
    ])

    app.run(host=host, debug=debug, port=str(port), jupyter_mode='external')

    print(f"Dash app available at {app_url}")


async def run_cloud_function(
    app: 'FastAPI',
    kill_existing_server: bool = True,
    host: str = '0.0.0.0',
    log_level: str = 'error',
) -> None:
    from ._uvicorn_util import AwaitableUvicornServer
    try:
        import uvicorn
    except ImportError:
        raise ImportError('package uvicorn is required to run cloud functions')

    port = portal.app_listen_port
    app_url = portal.app_url

    if port is None or app_url is None:
        raise RuntimeError(
           'Portal not fully initialized. '
           ' Is the code running outside SingleStoreDB notebook environment?',
        )

    if kill_existing_server:
        _kill_process_by_port(port)

    base_path = urllib.parse.urlparse(app_url).path
    app.root_path = base_path

    config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
    server = AwaitableUvicornServer(config)

    asyncio.create_task(server.serve())
    await server.wait_for_startup()

    print(f"Cloud function available at {app_url}")


def _kill_process_by_port(port: int) -> None:
    existing_process = _find_process_by_port(port)
    kernel_pid = os.getpid()
    # Make sure we are not killing current kernel
    if existing_process is not None and kernel_pid != existing_process.pid:
        print(f"Killing process {existing_process.pid} which is using port {port}")
        os.kill(existing_process.pid, signal.SIGKILL)


def _find_process_by_port(port: int) -> 'Process | None':
    try:
        import psutil
    except ImportError:
        raise ImportError('package psutil is required')

    for proc in psutil.process_iter(['pid']):
        try:
            connections = proc.connections()
            for conn in connections:
                if conn.laddr.port == port:
                    return proc
        except psutil.AccessDenied:
            pass

    return None
