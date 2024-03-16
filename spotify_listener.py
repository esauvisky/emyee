import asyncio
import aiohttp
import time
from spotipy.oauth2 import SpotifyOAuth
from app import SPOTIFY_SCOPE
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
        self.spotify_auth = SpotifyOAuth(client_id=client_id,
                                         client_secret=client_secret,
                                         redirect_uri=SPOTIFY_REDIRECT_URI,
                                         scope=SPOTIFY_SCOPE)

    async def listen(self):
        # Start the background task for fetching updates from Spotify
        asyncio.create_task(self.fetch_spotify_changes())

        while True:
            # Update the progress based on the delay
            if self.current_progress > SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY:
                current_progress = self.current_progress + time.time() - self.last_api_update_time
                while self.events_queue.full():
                    current_progress = self.current_progress + time.time() - self.last_api_update_time + SPOTIFY_CHANGES_LISTENER_DELAY
                    await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_DELAY)
                self.events_queue.put_nowait(EventAdjustProgressTime(current_progress))
            await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_DELAY)

    async def fetch_spotify_changes(self):
        loop = asyncio.get_running_loop()
        while True:
            # Ensure enough time has passed before making another API call
            if time.time() - self.last_api_update_time >= API_REQUEST_INTERVAL:
                access_token = await loop.run_in_executor(None, lambda: prompt_for_user_token(
                    self.user_id,
                    SPOTIFY_SCOPE,
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    redirect_uri=SPOTIFY_REDIRECT_URI,
                ))

                if not access_token:
                    logger.error("Failed to retrieve Spotify token.")
                    await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY)
                    continue

                headers = {'Authorization': f"Bearer {access_token}"}
                async with aiohttp.ClientSession(headers=headers) as session:
                    current_playing = await self._get_current_playing(session)
                    if not current_playing.get('is_playing', False):
                        self.current_track_id = None
                        await self.events_queue.put(EventStop())
                    elif current_playing['item']['id'] != self.current_track_id:
                        self.current_track_id = current_playing['item']['id']
                        analysis = await self._get_audio_analysis(session, self.current_track_id)
                        self.current_progress = current_playing['progress_ms'] / 1000
                        await self.events_queue.put(EventSongChanged(analysis, self.current_progress))
                    self.last_api_update_time = time.time()
                    self.current_progress = current_playing['progress_ms'] / 1000

            await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_DELAY)  # Small sleep to prevent tight loop in case of errors

    async def _get_current_playing(self, session):
        async with session.get(API_CURRENT_PLAYING) as response:
            return await response.json()

    async def _get_audio_analysis(self, session, track_id):
        async with session.get(f"{API_AUDIO_ANALYSIS}{track_id}") as response:
            return await response.json()

    def _get_start_time(self, current_playing, request_time):
        # spotify timestamp appears to be incorrect https://github.com/spotify/web-api/issues/640
        return (request_time + time.time()) / 2 - current_playing['progress_ms'] / 1000
