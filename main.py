import os
import threading
from flask import Flask

flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Quotex signal bot is running."

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    # Start Flask in background thread
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    # Run bot in main thread (owns the event loop)
    from bot import main as run_bot
    run_bot()
    
