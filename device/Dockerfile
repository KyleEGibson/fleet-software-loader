FROM python:3.12-alpine
WORKDIR /device
COPY agent.py .
RUN adduser -D device
USER device
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=3 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/status || exit 1
CMD ["python", "-u", "agent.py"]
