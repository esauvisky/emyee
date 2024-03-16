import asyncio
import aiohttp
import time
import sys
from spotipy.oauth2 import SpotifyOAuth
from app import CONTROLLER_TICK, SPOTIFY_SCOPE
from models import EventSongChanged, EventAdjustProgressTime, EventStop
from loguru import logger
from spotipy.util import prompt_for_user_token

from utils import API_REQUEST_INTERVAL, API_AUDIO_ANALYSIS, API_CURRENT_PLAYING
from utils import SPOTIFY_CHANGES_LISTENER_DELAY, SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY, SPOTIFY_REDIRECT_URI


class SpotifyChangesListener:
    def __init__(self, user_id, client_id, client_secret, events_queue: asyncio.Queue):
        self.user_id = user_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.events_queue = events_queue
        self.current_track_id = None
        self.current_progress = 0  # Initial progress in seconds
        self.last_api_update_time = 0
        self.last_progress = 0
        self.headers = {}
        self.spotify_auth = SpotifyOAuth(client_id=client_id,
                                         client_secret=client_secret,
                                         redirect_uri=SPOTIFY_REDIRECT_URI,
                                         scope=SPOTIFY_SCOPE)

    async def listen(self):
        # Start the background task for fetching updates from Spotify
        asyncio.create_task(self.fetch_spotify_changes())
        await asyncio.sleep(1)
        while True:
            await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_DELAY)
            new_progress = self.current_progress + time.time() - self.last_api_update_time
            if self.last_progress > new_progress and self.last_progress - new_progress < SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY:
                continue
            await self.events_queue.put(EventAdjustProgressTime(new_progress))
            self.last_progress = new_progress

    async def fetch_spotify_changes(self):
        access_token = prompt_for_user_token(
            self.user_id,
            SPOTIFY_SCOPE,
            client_id=self.client_id,
            client_secret=self.client_secret,
            redirect_uri=SPOTIFY_REDIRECT_URI,
        )
        if not access_token:
            logger.error("Failed to retrieve Spotify token.")
            sys.exit(1)
        self.headers = {'Authorization': f"Bearer {access_token}"}
        while True:
            # Ensure enough time has passed before making another API call
            if time.time() - self.last_api_update_time >= API_REQUEST_INTERVAL:
                async with aiohttp.ClientSession(headers=self.headers) as session:
                    before_request = time.time()
                    current_playing = await self._get_current_playing(session)
                    if not current_playing.get('is_playing', False):
                        self.current_track_id = None
                        await self.events_queue.put(EventStop())
                        continue

                    self.current_progress = current_playing["progress_ms"] / 1000 - (time.time() - before_request)

                    if current_playing['item']['id'] != self.current_track_id:
                        self.current_track_id = current_playing['item']['id']
                        analysis = await self._get_audio_analysis(session, self.current_track_id)
                        await self.events_queue.put(EventSongChanged(analysis, self.current_progress))
                    self.last_api_update_time = time.time()
            await asyncio.sleep(0)

    async def _get_current_playing(self, session):
        async with session.get(API_CURRENT_PLAYING) as response:
            return await response.json()

    async def _get_audio_analysis(self, session, track_id):
        async with session.get(f"{API_AUDIO_ANALYSIS}{track_id}") as response:
            return await response.json()

    def _get_start_time(self, current_playing, request_time):
        # spotify timestamp appears to be incorrect https://github.com/spotify/web-api/issues/640
        return (request_time + time.time()) / 2 - current_playing['progress_ms'] / 1000
