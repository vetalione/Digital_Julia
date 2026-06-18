import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Модели нейросетей для генерации сценариев
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-pro-latest")

# Сколько бесплатных сценариев доступно до оплаты
FREE_SCENARIO_LIMIT = int(os.getenv("FREE_SCENARIO_LIMIT", "3"))

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
RAILWAY_PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
