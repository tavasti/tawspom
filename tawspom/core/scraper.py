import time
import random
from typing import List, Dict
from playwright.sync_api import sync_playwright

class SpotifyScraper:
    def __init__(self):
        self.browser_args = [
            "--disable-blink-features=AutomationControlled",
        ]

    def get_related_artists(self, artist_id: str) -> List[Dict[str, str]]:
        """Uses Playwright to scrape 'Fans Also Like' from the Spotify web player."""
        url = f"https://open.spotify.com/artist/{artist_id}/related"
        related_artists = []

        with sync_playwright() as p:
            print(f"  Launching robot to visit: {url}")
            # Launch headless browser
            browser = p.chromium.launch(headless=True)
            # Use a realistic context
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            try:
                # Navigate and wait for content
                page.goto(url, wait_until="networkidle", timeout=30000)
                
                # Spotify loads content dynamically. We wait for artist links to appear.
                # The related artists are typically in <a> tags with data-testid="artist-card" or similar.
                # We'll use a more generic selector for artist links on that page.
                print("  Waiting for React app to render content...")
                page.wait_for_selector('a[href*="/artist/"]', timeout=10000)
                
                # Give it a tiny bit more time to ensure all cards are rendered
                time.sleep(2)
                
                # Find all artist links
                # Usually they look like <a href="/artist/12345">Artist Name</a>
                elements = page.query_selector_all('a[href*="/artist/"]')
                
                seen_ids = set()
                for el in elements:
                    href = el.get_attribute("href")
                    name = el.inner_text().strip()
                    
                    # Extract ID from /artist/ID or /artist/ID/something
                    match = re.search(r'/artist/([a-zA-Z0-9]+)', href)
                    if match:
                        a_id = match.group(1)
                        # Avoid the main artist and duplicates
                        if a_id != artist_id and a_id not in seen_ids and name:
                            # We check if name is just a number or empty
                            if not name.isdigit():
                                related_artists.append({"id": a_id, "name": name})
                                seen_ids.add(a_id)
                
                print(f"  Robot successfully discovered {len(related_artists)} related artists.")
                
            except Exception as e:
                print(f"  Robot encountered an error: {e}")
            finally:
                browser.close()
                
        return related_artists

# Add regex import since it's used
import re
