# Go live — team UI

The Streamlit app (`app.py`) exposes **two workflows**:

| Workflow | When to use | What to upload |
|----------|-------------|----------------|
| **A · Fire Water FVI** | Jimcel Fire Water reports (e.g. UBIT-GP) | **One** full inspection PDF |
| **B · Process Piping** | EDOP-QP (and similar) process piping packs | **1** summary/checklist PDF **+** all **line-number** PDFs |

Logic (unchanged from CLI):

- **A:** observation tables → rows; crop photos/ISO; judgment columns blank.
- **B:** summary From–To → Line Description; line PDF captions under images → Anomaly; crop photos; no images → `no data`.

These PDFs are inspection reports. Prefer a **password** and a host your company allows.

```bash
export APP_PASSWORD='a-shared-team-password'
```

---

## Why Railway (not Vercel)

- Needs **Docker + Tesseract**, multi-minute jobs, and uploads up to hundreds of MB (Workflow B).
- **Vercel** is for short serverless HTTP — not suitable.
- **Railway** (or Render / a company VM) runs the Dockerfile with a persistent Streamlit process.

---

## Option A — Share from your Mac (quickest, same office/VPN)

```bash
cd ~/Developer/rbws-fvi-extractor
source .venv/bin/activate
pip install -r requirements.txt pytesseract
export PYTHONPATH=src
export APP_PASSWORD='choose-a-password'
streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Teammates open `http://YOUR_IP:8501` while your Mac is running the app.

---

## Option B — Railway (recommended public URL)

1. Push this repo to GitHub.
2. Install CLI: `npm i -g @railway/cli` (or see [railway.app](https://railway.app)).
3. `railway login` → `railway init` → `railway up`.
4. Set variable `APP_PASSWORD` in the Railway project.
5. Generate a public domain in Railway → Networking.
6. Share the URL + password.

Dockerfile already binds Streamlit to `0.0.0.0` and `${PORT:-8501}`.

---

## Option C — Streamlit Community Cloud

Works for lighter **Workflow A** jobs. Workflow B (many line PDFs) often hits free-tier timeouts/memory — prefer Railway.

1. Push to GitHub → [share.streamlit.io](https://share.streamlit.io) → Main file `app.py`.
2. Secrets:

   ```toml
   APP_PASSWORD = "shared-team-password"
   ```

---

## Option D — Docker on a company VM

```bash
docker build -t rbws-fvi .
docker run --rm -p 8501:8501 -e APP_PASSWORD='shared-team-password' rbws-fvi
```

---

## What teammates do

1. Open the URL and enter the password (if set).
2. Choose **Workflow A** or **Workflow B** in the sidebar.
3. Upload the required PDF(s).
4. Click **Generate workbook** → **Download RBWS Excel**.

No Python install on their machines.
