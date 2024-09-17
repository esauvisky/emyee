import asyncio
from loguru import logger

class CustomLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.holder = None  # Reference to the task holding the lock

    async def acquire(self):
        await self.lock.acquire()
        self.holder = asyncio.current_task()
        logger.trace(f"Lock acquired by task {self.holder.get_name()}.")

    def release(self):
        logger.trace(f"Lock released by task {self.holder.get_name() if self.holder else 'Unknown'}.")
        self.holder = None
        self.lock.release()

    def locked(self):
        return self.lock.locked()
