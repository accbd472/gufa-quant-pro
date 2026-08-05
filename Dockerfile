FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
RUN useradd --create-home --uid 10001 gufa \
    && mkdir -p /app/runtime /config \
    && chown -R gufa:gufa /app /config

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gufa_quant_pro.py gufa_calendar.py gufa_paipan.py gufa_paipan_signal.py \
     gufa_paipan_qimen.py gufa_paipan_liuren.py gufa_paipan_taiyi.py \
     gufa_paipan_bazi.py gufa_paipan_ziwei.py gufa_paipan_yijing.py \
     gufa_yijing_data.py config.example.json ./

USER gufa

STOPSIGNAL SIGTERM
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "gufa_quant_pro.py", "--config", "/config/config.json", "status"]

ENTRYPOINT ["python", "gufa_quant_pro.py"]
CMD ["--config", "/config/config.json", "run"]
