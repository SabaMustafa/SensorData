"""
Start the server:  python run.py

Optional HTTPS (needed for the phone camera on /mobile):
put cert.pem and key.pem in the certs/ folder, or set SSL_CERT / SSL_KEY in .env.
"""
import os

from app import create_app
from config import BASE_DIR

app = create_app()

if __name__ == "__main__":
    cert = os.getenv("SSL_CERT", os.path.join(BASE_DIR, "certs", "cert.pem"))
    key = os.getenv("SSL_KEY", os.path.join(BASE_DIR, "certs", "key.pem"))
    ssl_context = (cert, key) if os.path.exists(cert) and os.path.exists(key) else None

    app.run(
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
        debug=False,
        use_reloader=False,  # the reloader would start the camera thread twice
        threaded=True,       # needed: each video viewer keeps a connection open
        ssl_context=ssl_context,
    )
