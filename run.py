"""Start the help desk server."""
import socket
import uvicorn
from app import config


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    print("\n  Help Desk")
    print(f"  This machine   http://localhost:{config.PORT}")
    print(f"  Other machine  http://{lan_ip()}:{config.PORT}")
    print(f"  Database       {config.DB_PATH}")
    print(f"  Password       {'set' if config.PASSWORD else 'NOT SET - anyone on the LAN can read this'}\n")
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)
