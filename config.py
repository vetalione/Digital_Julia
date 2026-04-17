import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

CONSULTATION_URL = os.getenv("CONSULTATION_URL", "https://tribute.to/yuliya-consultation")
COURSE_URL = os.getenv("COURSE_URL", "https://tribute.to/yuliya-course")

# PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Tribute
TRIBUTE_API_KEY = os.getenv("TRIBUTE_API_KEY", "")
TRIBUTE_PRODUCT_LINK = os.getenv("TRIBUTE_PRODUCT_LINK", "")
ACCESS_DURATION_DAYS = int(os.getenv("ACCESS_DURATION_DAYS", "30"))

# Railway
PORT = int(os.getenv("PORT", "8080"))
