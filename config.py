# config.py

# Redis configuration for Celery Broker and Result Backend
# Example: 'redis://localhost:6379/0'
REDIS_URL = 'redis://:Order123@localhost:6380/1'

# Base URL of the target non-queue API service
# Example: 'http://your-target-api.com'
TARGET_API_BASE_URL = 'http://localhost:5000' # Placeholder - Change this

# Secret key for Flask session management (optional but recommended)
SECRET_KEY = 'NjdkMTg0NTNkNWQ2ODcyMmJjNGM4ZTk5ZDZiZDM4ZTMwNmY2NWEwMw==' # Change this to a random secret key 