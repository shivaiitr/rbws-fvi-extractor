# Go live — team UI

The Streamlit app (`app.py`) is the team UI: upload PDF → Generate Excel → download.

These PDFs are inspection reports. Prefer a **password** and a host your company allows. Do not put them on a fully public site with no login.

Set a shared password:

```bash
export APP_PASSWORD='a-shared-team-password'
```

---

## Option A — Share from your Mac (quickest, same office/VPN)

Good for a trial with 1–2 people on the same network.

```bash
cd ~/Developer/rbws-fvi-extractor
source .venv/bin/activate
pip install -r requirements.txt pytesseract
export PYTHONPATH=src
export APP_PASSWORD='choose-a-password'
streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Then:

1. Find your Mac’s IP (System Settings → Network, or `ipconfig getifaddr en0`).
2. Teammates open `http://YOUR_IP:8501` **while your Mac is on and the command is running**.
3. If it fails, allow port 8501 in macOS Firewall.

This is **not** the internet. It only works on LAN/VPN while the process is up.

---

## Option B — Streamlit Community Cloud (public URL)

Needs a GitHub repo.

1. Push this project to GitHub (private repo is fine).
2. Go to [https://share.streamlit.io](https://share.streamlit.io) and sign in with GitHub.
3. **New app** → pick the repo → Main file `app.py`.
4.    In **Secrets** add:

   ```toml
   APP_PASSWORD = "shared-team-password"
   ```

   The app reads `APP_PASSWORD` from Streamlit secrets or the environment.
5. After deploy, share the `https://….streamlit.app` URL + password.

Limits: free Cloud has timeouts; large PDFs with images can exceed them. Native-text reports (YOHO-style) are safer than 40-page scanned PDFs.

---

## Option C — Docker on a company VM / Railway / Render

Most reliable for Tesseract + longer jobs.

**On a Linux VM you control:**

```bash
docker build -t rbws-fvi .
docker run --rm -p 8501:8501 -e APP_PASSWORD='shared-team-password' rbws-fvi
```

Open `http://SERVER_IP:8501`. Put nginx + HTTPS in front if this is a real team service.

**Railway / Render / Fly.io:** connect the GitHub repo, Dockerfile, env `APP_PASSWORD`, open port 8501 (or set `PORT` if the host requires it). Some hosts need:

```
CMD streamlit run app.py --server.port $PORT --server.address 0.0.0.0
```

---

## What teammates do

1. Open the URL.
2. Enter the password (if set).
3. Upload one FVI PDF.
4. Click **Generate Excel**.
5. Download `{name}_RBWS.xlsx`.

No Python install on their machines.

---

## What you must tell them

- One PDF at a time.
- Processing can take several minutes (render + OCR + photo crops).
- Judgment columns stay blank on purpose.
- Do not upload reports to a public app unless the host and password are approved internally.
