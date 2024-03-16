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
    def __init__(self, user_id, client_id, client_secret, events_queue):
        self.user_id = user_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.events_queue = events_queue
        self.last_request_time = 0
        self.spotify_auth = SpotifyOAuth(client_id=self.client_id,
                                         client_secret=self.client_secret,
                                         redirect_uri=SPOTIFY_REDIRECT_URI,
                                         scope=SPOTIFY_SCOPE)

    async def listen(self):
        while True:
            access_token = prompt_for_user_token(self.user_id,
                                        SPOTIFY_SCOPE,
                                        client_id=self.client_id,
                                        client_secret=self.client_secret,
                                        redirect_uri=SPOTIFY_REDIRECT_URI)
            if not access_token:
                logger.error("Failed to retrieve Spotify token.")
                await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY)
                continue

            headers = {'Authorization': f"Bearer {access_token}"}
            async with aiohttp.ClientSession(headers=headers) as session:
                try:
                    await self._listen_to_spotify_changes(session)
                except Exception as e:
                    logger.exception(f"Something went wrong with spotify_changes_listener: {e}")
                    await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY)

    async def _listen_to_spotify_changes(self, session):
        current_id = None
        while True:
            # request_time = asyncio.get_event_loop().time()
            current_playing = await self._get_current_playing(session)
            if not current_playing.get('is_playing', False):
                current_id = None
                await self.events_queue.put(EventStop())
            elif current_playing['item']['id'] != current_id:
                current_id = current_playing['item']['id']
                analysis = await self._get_audio_analysis(session, current_id)
                progress = current_playing['progress_ms'] / 1000
                await self.events_queue.put(EventSongChanged(analysis, progress))
            else:
                progress = current_playing['progress_ms'] / 1000
                await self.events_queue.put(EventAdjustProgressTime(progress))

            await asyncio.sleep(SPOTIFY_CHANGES_LISTENER_DELAY)


    async def _get_current_playing(self, session):
        async with session.get(API_CURRENT_PLAYING) as response:
            return await response.json()

    async def _get_audio_analysis(self, session, track_id):
        async with session.get(f"{API_AUDIO_ANALYSIS}{track_id}") as response:
            return await response.json()

    def _get_start_time(self, current_playing, request_time):
        # spotify timestamp appears to be incorrect https://github.com/spotify/web-api/issues/640
        return (request_time + time.time()) / 2 - current_playing['progress_ms'] / 1000
