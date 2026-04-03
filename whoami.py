from tawspom.core.spotify import SpotifyClient

def test_auth():
    try:
        client = SpotifyClient()
        user = client.sp.current_user()
        print(f"\nSuccessfully authenticated!")
        print(f"User Display Name: {user['display_name']}")
        print(f"User ID: {user['id']}")
        print(f"Country: {user['country']}")
    except Exception as e:
        print(f"\nAuthentication failed: {e}")

if __name__ == "__main__":
    test_auth()
