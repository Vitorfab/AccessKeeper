import os
import threading
import webbrowser

from waitress import serve

from accesskeeper import create_app

app = create_app()

if __name__ == "__main__":
    host = app.config["HOST"]
    port = app.config["PORT"]
    url = f"http://{host}:{port}"
    print(f"AccessKeeper is running at {url}  (press Ctrl+C to stop)")
    if os.environ.get("OPEN_BROWSER") == "1":
        threading.Timer(1.5, webbrowser.open, args=[url]).start()
    serve(app, host=host, port=port, threads=4)
