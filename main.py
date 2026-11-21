import sys

from tawspom.cli import main
from tawspom.core.spotify import SpotifyRateLimitError

if __name__ == "__main__":
    try:
        main()
    except SpotifyRateLimitError as e:
        print(f"\n⏳ {e}")
        sys.exit(1)
