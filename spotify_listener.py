import asyncio
import aiohttp
import time
import sys
from urllib.parse import urlparse, urlunparse
from spotipy.oauth2 import SpotifyOAuth
from spotipy import exceptions as spotipy_exceptions
from models import EventSongChanged, EventAdjustProgressTime, EventStop
from loguru import logger

from utils import API_REQUEST_INTERVAL, API_AUDIO_ANALYSIS, API_CURRENT_PLAYING, SPOTIFY_SCOPE
from utils import SPOTIFY_CHANGES_LISTENER_DELAY, SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY, SPOTIFY_REDIRECT_URI


class SpotifyChangesListener:
    def __init__(self, user_id, client_id, client_secret, events_queue: asyncio.Queue):
        self.user_id = user_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.events_queue = events_queue
        self.current_track_id = None
        self.current_progress = 0
        self.last_api_update_time = 0
        self.last_progress = 0
        self._progress_anchor_mono = time.monotonic()
        self._progress_anchor_value = 0.0
        self._progress_correction = 0.0
        self._progress_correction_target = 0.0
        self._progress_last_emit_mono = self._progress_anchor_mono
        self.headers = {}
        self.poll_interval: float = SPOTIFY_CHANGES_LISTENER_DELAY
        self.spotify_auth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=SPOTIFY_REDIRECT_URI,
            scope=SPOTIFY_SCOPE,
        )

    async def listen(self):
        # Run Spotify polling and progress updates together to avoid orphaned tasks.
        await asyncio.gather(
            self.fetch_spotify_changes(),
            self._emit_progress(),
        )

    async def _emit_progress(self):
        await asyncio.sleep(1)
        waiting_for_api_sync = False
        while True:
            now_mono = time.monotonic()
            elapsed = now_mono - self._progress_last_emit_mono
            self._progress_last_emit_mono = now_mono
            slew = min(1.0, max(0.0, elapsed / 2.0))
            self._progress_correction += (
                self._progress_correction_target - self._progress_correction
            ) * slew
            new_progress = self._progress_anchor_value + (
                now_mono - self._progress_anchor_mono
            ) + self._progress_correction
            if self.last_progress > new_progress and self.last_progress - new_progress < SPOTIFY_CHANGES_LISTENER_FAILURE_DELAY:
                if not waiting_for_api_sync:
                    logger.debug("Waiting for Spotify progress reference to catch up")
                    waiting_for_api_sync = True
                await asyncio.sleep(self.poll_interval)
                continue
            waiting_for_api_sync = False
            await self.events_queue.put(EventAdjustProgressTime(new_progress))
            self.last_progress = new_progress
            await asyncio.sleep(self.poll_interval)

    async def fetch_spotify_changes(self):
        access_token = self._resolve_access_token()
        if not access_token:
            logger.error("Failed to retrieve Spotify token.")
            sys.exit(1)
        self.headers = {'Authorization': f"Bearer {access_token}"}

        while True:
            if time.time() - self.last_api_update_time >= API_REQUEST_INTERVAL:
                fresh_token = self.spotify_auth.validate_token(self.spotify_auth.get_cached_token())
                if fresh_token:
                    self.headers = {'Authorization': f"Bearer {fresh_token['access_token']}"}
                async with aiohttp.ClientSession(headers=self.headers) as session:
                    before_request = time.time()
                    current_playing = await self._get_current_playing(session)
                    # Capture immediately after response — used as the reference time for
                    # both current_progress and last_api_update_time so they stay consistent.
                    receive_time = time.time()
                    api_rtt = receive_time - before_request

                    if current_playing is None:
                        if self.current_track_id is not None:
                            self.current_track_id = None
                            await self.events_queue.put(EventStop())
                        self.last_api_update_time = receive_time
                        continue

                    if not current_playing.get('is_playing', False):
                        self.current_track_id = None
                        await self.events_queue.put(EventStop())
                        self.last_api_update_time = receive_time
                        continue

                    # progress_ms was captured server-side at ~before_request + rtt/2.
                    # Add rtt/2 to get the estimated position at receive_time.
                    reported_progress = current_playing["progress_ms"] / 1000 + api_rtt / 2
                    now_mono = time.monotonic()
                    predicted_progress = self._progress_anchor_value + (
                        now_mono - self._progress_anchor_mono
                    ) + self._progress_correction
                    self._progress_correction_target += reported_progress - predicted_progress
                    self.current_progress = reported_progress
                    item = current_playing.get('item') or {}

                    if item.get('type') != 'track':
                        if self.current_track_id != item.get('id'):
                            logger.warning("Ignoring non-track Spotify item type: {}", item.get('type'))
                            self.current_track_id = item.get('id')
                            await self.events_queue.put(EventStop())
                        self.last_api_update_time = receive_time
                        continue

                    current_track_id = item.get('id')
                    if current_track_id != self.current_track_id:
                        self._progress_anchor_value = reported_progress
                        self._progress_anchor_mono = now_mono
                        self._progress_correction = 0.0
                        self._progress_correction_target = 0.0
                        self._progress_last_emit_mono = now_mono
                        if self.current_track_id is not None:
                            await self.events_queue.put(EventStop())
                        self.current_track_id = current_track_id
                        song_name = item.get('name', 'Unknown')
                        artist_names = ', '.join(a.get('name', '') for a in item.get('artists', []))
                        album_name = (item.get('album') or {}).get('name', '')
                        logger.info(
                            "Now playing: {} — {} | album={} | id={} | progress={:.2f}s",
                            song_name, artist_names or 'Unknown artist', album_name or 'Unknown album',
                            current_track_id, self.current_progress,
                        )
                        analysis = await self._get_audio_analysis(session, current_track_id)
                        if analysis is None:
                            logger.warning("No audio analysis available for track id {}", current_track_id)
                            await self.events_queue.put(EventStop())
                            self.last_api_update_time = receive_time
                            continue
                        await self.events_queue.put(EventSongChanged(
                            analysis,
                            self.current_progress,
                            song_name=song_name,
                            artist_names=artist_names,
                            album_name=album_name,
                            track_id=current_track_id,
                        ))
                    self.last_api_update_time = receive_time
                await asyncio.sleep(self.poll_interval)
            await asyncio.sleep(0)

    def _resolve_access_token(self):
        try:
            token = self.spotify_auth.get_access_token(as_dict=False, check_cache=True)
        except spotipy_exceptions.SpotifyOauthError as err:
            error_text = str(err).lower()
            if "invalid_redirect_uri" in error_text or "invalid redirect uri" in error_text:
                logger.error(
                    "Spotify rejected redirect URI {}. Add this exact URI to your Spotify app settings "
                    "and try again.",
                    SPOTIFY_REDIRECT_URI
                )
                alt_uri = self._alternative_redirect_uri()
                if alt_uri:
                    logger.warning("Trying alternate redirect URI {} before giving up.", alt_uri)
                    self.spotify_auth.redirect_uri = alt_uri
                    return self._manual_oauth_with_paste(open_browser=False)
                return None
            logger.error("Spotify OAuth failed while resolving token: {}", err)
            raise
        except OSError as err:
            if getattr(err, "errno", None) == 98:
                logger.warning(
                    "Spotify callback listener port conflict at {}. "
                    "Stop the existing process and retry, or use manual URL paste flow below.",
                    SPOTIFY_REDIRECT_URI,
                )
                token = None
            else:
                logger.error("Unexpected error retrieving Spotify token: {}", err)
                raise

        if token:
            return token

        logger.warning("No valid cached Spotify token found.")

        if not sys.stdin.isatty():
            logger.error(
                "No interactive terminal available and no cached Spotify token exists. "
                "Run this command interactively once to complete login."
            )
            return None

        logger.warning(
            "Spotify local auth callback flow could not be used automatically. "
            "Close any other process using {} and retry if this happens repeatedly.",
            SPOTIFY_REDIRECT_URI
        )
        token = self._manual_oauth_with_paste()
        if token:
            return token

        return None

    def _manual_oauth_with_paste(self, open_browser: bool = True):
        return self._prompt_for_oauth_response(open_browser=open_browser)

    def _prompt_for_oauth_response(self, open_browser: bool = True):
        # Keep the existing "paste redirected URL" flow for environments where
        # the callback listener port is already occupied.
        original_open_browser = self.spotify_auth.open_browser
        self.spotify_auth.open_browser = open_browser
        try:
            return self.spotify_auth.get_access_token(
                as_dict=False,
                check_cache=False,
            )
        except spotipy_exceptions.SpotifyOauthError as err:
            logger.error(
                "OAuth failed: {}. "
                "After logging in, paste the full redirected URL shown by Spotify.",
                err
            )
            return None
        finally:
            self.spotify_auth.open_browser = original_open_browser

    def _alternative_redirect_uri(self):
        parsed = urlparse(SPOTIFY_REDIRECT_URI)
        if parsed.scheme not in ("http", "https"):
            return None
        host = parsed.hostname or ""
        if host == "localhost":
            host = "127.0.0.1"
        elif host == "127.0.0.1":
            host = "localhost"
        else:
            return None
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        return urlunparse((parsed.scheme, netloc, "/", "", "", ""))

    async def _get_current_playing(self, session):
        async with session.get(API_CURRENT_PLAYING) as response:
            if response.status == 204:
                return None
            response.raise_for_status()
            try:
                return await response.json()
            except aiohttp.ContentTypeError:
                response_body = await response.text()
                logger.warning("Unexpected content type in currently-playing response: {}", response_body[:200])
                return None

    async def _get_audio_analysis(self, session, track_id):
        async with session.get(f"{API_AUDIO_ANALYSIS}{track_id}") as response:
            if response.status == 404:
                return None
            response.raise_for_status()
            return await response.json()
