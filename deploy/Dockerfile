# feedback-hub submission server.
#
# One small process behind a reverse proxy, holding the only GitHub token, so
# that a static page can file an issue for somebody who has no GitHub account.
# See src/feedback_hub/server.py for why that is the whole point.
#
# Built from the repository root:
#   docker compose -f deploy/docker-compose.yml build
#
FROM python:3.13-slim

# Only gunicorn is added. feedback-hub itself has no dependencies and the
# server is a plain WSGI callable on the standard library, which is what keeps
# this image small enough to rebuild without thinking about it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . "gunicorn>=21.2"

# Runs as nobody. A process whose entire job is to accept anonymous input from
# the public internet has no business owning anything on the filesystem.
#
# HOME is set because nobody's home directory is /nonexistent, and gunicorn's
# control server tries to open a socket under it -- logging a permission error
# on every boot. Harmless, but a recurring ERROR in the log is how a real one
# gets missed.
ENV HOME=/tmp
USER 65534:65534

EXPOSE 8095

# Two workers, not one: a single worker means one slow GitHub call blocks the
# next visitor's submission. Not more, because the rate limiter counts in
# memory per process -- with N workers the effective limit is N per minute per
# address, which is why this stays small and deliberate rather than scaling.
# See RateLimiter's docstring.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8095", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "30", \
     "--graceful-timeout", "20", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "feedback_hub.server:application"]
