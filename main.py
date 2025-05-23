from tawspom.db import init_db
from tawspom.auth import get_spotify_client

def main():
    init_db()
    sp = get_spotify_client()
    
    current_user = sp.current_user()
    print(f"Logged in as: {current_user['display_name']}")

if __name__ == "__main__":
    main()

