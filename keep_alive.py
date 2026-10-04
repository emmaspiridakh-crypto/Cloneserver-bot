import os
import threading

from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return "ok"


def keep_alive():
    port = int(os.environ.get("PORT", 1000))
    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port),
        daemon=True,
    ).start()
