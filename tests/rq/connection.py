import os

from redis import Redis

#: The self-tests' Redis database, emptied around every case: REDIS_URL, or database 14 on localhost.
CONNECTION = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/14"))
