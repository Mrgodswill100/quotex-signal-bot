import threading
import os
from server import app
from bot import main as run_bot

def run_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    t = threading.Thread(target=run_server, daemon=True)
    t.start()
    run_bot()
  
