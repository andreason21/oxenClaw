# Install on Windows (via WSL2)

sampyClaw is a Linux/macOS service. On Windows you run it inside **WSL2**
— the Microsoft-supported Linux subsystem. Windows native (without WSL)
is not supported because sandboxing, signal handling, and the Linux
networking stack the gateway depends on don't have direct equivalents
on Win32.

This guide takes you from a fresh Windows install to a running
sampyClaw gateway with Ollama-backed local LLM, in 15–25 minutes.

[**한국어 ↓**](#한국어)

---

## 1. Prerequisites

- **Windows 11**, or **Windows 10 build 19044+** (most current installs).
- **Admin rights** on first-time WSL setup.
- **8 GB+ RAM**; 16 GB recommended if running a 7B-class model alongside other apps.
- **Internet** for `pip install`, `ollama pull`, and Telegram outbound (if used).
- **NVIDIA GPU** is optional — Ollama runs on CPU just fine for the recommended `gemma4:latest`.

---

## 2. Install WSL2 + Ubuntu

In an **elevated PowerShell** (right-click → Run as administrator):

```powershell
wsl --install -d Ubuntu-22.04
```

Reboot if prompted, then launch "Ubuntu 22.04" from the Start menu and
finish the first-run setup (username + password). After that everything
below happens in the **Ubuntu shell**, not PowerShell.

Verify WSL2 (not WSL1):

```powershell
wsl -l -v
```

`VERSION` should be `2`. If it reads `1`, convert it:

```powershell
wsl --set-version Ubuntu-22.04 2
```

> **WSL1 is not supported.** WSL1 lacks `/proc/self/fd` and full signal
> semantics. Always use WSL2.

---

## 3. Install Python 3.11+

Ubuntu 22.04 ships Python 3.10. sampyClaw needs **3.11+**. Install via
the deadsnakes PPA:

```bash
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev build-essential git
```

Verify:

```bash
python3.12 --version    # → Python 3.12.x
```

> Ubuntu 24.04 ships Python 3.12 natively — skip the PPA step there.

---

## 4. Install Ollama

You have **two options**. Option A is recommended for simplicity.

### Option A (recommended): Ollama inside WSL2

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Start it as a foreground service (or run in background):

```bash
ollama serve &
```

Pull the recommended default model:

```bash
ollama pull gemma4:latest
```

Confirm it's reachable on `127.0.0.1:11434`:

```bash
curl -s http://127.0.0.1:11434/api/tags | head
```

### Option B: Ollama on the Windows host

Install [Ollama for Windows](https://ollama.com/download/windows). It
listens on `127.0.0.1:11434` from Windows' perspective. **WSL2 cannot
reach Windows `127.0.0.1` directly** — you need to reach it via the
Windows host IP.

Find the host IP from inside WSL2:

```bash
ip route show default | awk '{print $3}'
# e.g. 172.28.176.1
```

Then start the gateway with `--base-url`:

```bash
sampyclaw gateway start \
  --provider local --model gemma4:latest \
  --base-url http://172.28.176.1:11434/v1
```

Or set `OLLAMA_HOST=0.0.0.0` on Windows so it listens on all interfaces;
WSL2 can then reach it via the host IP.

> **Modern WSL2 mirrored mode** (Windows 11 23H2+ with `.wslconfig`
> `networkingMode=mirrored`) makes `127.0.0.1` resolve to the Windows
> host as well. If you have it enabled, Option B works without the
> `--base-url` flag.

---

## 5. GPU acceleration (optional)

For NVIDIA GPUs:

1. Install the latest **NVIDIA Game Ready / Studio driver** on Windows
   (the driver, *not* a Linux driver inside WSL).
2. WSL2 detects it automatically. Confirm:
   ```bash
   nvidia-smi   # should list your GPU
   ```
3. Ollama picks it up on next start. `ollama serve` logs will show
   `CUDA driver detected`.

For AMD / Intel GPUs, see Ollama's GPU compatibility matrix — coverage
on WSL is partial.

---

## 6. Install sampyClaw

Clone into your **Linux home directory** (NOT under `/mnt/c/`, which
is 10–100× slower for `git` and `pip`):

```bash
cd ~
git clone https://github.com/andreason21/sampyClaw.git
cd sampyClaw

python3.12 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -e ".[dev]"
```

Sanity check:

```bash
sampyclaw paths
sampyclaw config validate
```

Run the test suite (takes ~10 seconds):

```bash
pytest -q
# 1035 passed, 10 skipped
```

---

## 7. Configure + start

Create `~/.sampyclaw/config.yaml`:

```yaml
channels: {}
agents:
  default:
    provider: local
    model: gemma4:latest
    system_prompt: |
      You are a helpful assistant.
```

Generate a token and start:

```bash
export SAMPYCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)
sampyclaw gateway start --provider local
```

You should see `gateway listening on http://127.0.0.1:7331`.

Open `http://127.0.0.1:7331/` in your **Windows browser** — WSL2
forwards `localhost` from Windows into the WSL VM by default, so the
URL "just works" without extra configuration.

---

## 8. (Optional) Bind for LAN access

If you want the gateway reachable from another machine on your LAN
(through the Windows host), bind to `0.0.0.0`:

```bash
sampyclaw gateway start --host 0.0.0.0 --port 7331 --provider local
```

Then open Windows port 7331 if your Windows Firewall blocks it. WSL2
forwards bound `0.0.0.0` ports out to the LAN automatically.

---

## 9. (Optional) Run as a service

WSL2 has supported systemd since Microsoft Store WSL 0.67.6. Enable in
`/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

Then `wsl --shutdown` from PowerShell, restart Ubuntu, and you can use
the systemd unit from [`docs/OPERATIONS.md`](OPERATIONS.md).

If systemd isn't available, run under `tmux` / `screen` / `nohup`:

```bash
nohup sampyclaw gateway start --provider local > ~/sampyclaw.log 2>&1 &
disown
```

---

## 10. Telegram on WSL2

Telegram bot inbound is **outbound-only** — your gateway long-polls
Telegram's servers. No port forwarding, no public IP, no Windows
Firewall changes required. Just put your token at
`~/.sampyclaw/credentials/telegram/main.json` and add the binding to
`config.yaml` exactly as in the main README.

---

## 11. Common gotchas

| Symptom | Cause / fix |
|---|---|
| `pip install` extremely slow, weird disk errors | You cloned into `/mnt/c/...`. Move to `~`. WSL2's NTFS bridge is much slower than ext4. |
| `Connection refused` to `127.0.0.1:11434` from sampyClaw | Ollama is running on Windows host, not in WSL2. Use Option B above (Windows host IP) or install Ollama in WSL2. |
| Browser can't reach `localhost:7331` from Windows | Make sure you bound to `127.0.0.1` (default) or `0.0.0.0`. Try `wsl --shutdown` and relaunch — sometimes the localhost forwarder gets stuck. |
| `pyright` or `pre-commit` reports CRLF errors | `git config --global core.autocrlf input` inside WSL2 to keep files LF. |
| `gpgsign` errors on `git commit` | WSL2 doesn't have your Windows GPG agent. Either disable signing (`git config --local commit.gpgsign false`) or set up `gpg` inside Ubuntu. |
| Power saving makes Ollama feel slow | Windows aggressive power throttling can starve WSL2. Set Windows power plan to "High performance" while running. |
| `ollama serve` says "Address already in use" | Another Ollama instance (often the Windows-host one) is bound to 11434. Either stop it (Task Manager → Ollama) or change WSL Ollama port via `OLLAMA_HOST=127.0.0.1:11435 ollama serve`. |

---

## 12. Verify everything works

```bash
# 1. Ollama responds
curl -s http://127.0.0.1:11434/api/tags | head

# 2. Gateway is up
curl -s http://127.0.0.1:7331/healthz

# 3. Readiness shows OK / degraded but not down
curl -s http://127.0.0.1:7331/readyz

# 4. Metrics scrape works
curl -s http://127.0.0.1:7331/metrics | head

# 5. End-to-end RPC via CLI
sampyclaw message send --agent default "say hi"
```

If all five succeed, you're production-ready on WSL2.

---

## 한국어

sampyClaw는 Linux/macOS 서비스. Windows에서는 **WSL2** 안에서 실행한다.
Win32 네이티브는 지원하지 않음 — 샌드박스, 시그널 처리, 게이트웨이가
의존하는 Linux 네트워크 스택의 동등 기능이 Windows에 없다.

이 가이드는 깨끗한 Windows에서 Ollama 기반 로컬 LLM이 동작하는
sampyClaw 게이트웨이까지 15–25분 소요.

### 1. 사전 요구사항

- **Windows 11**, 또는 **Windows 10 빌드 19044+**
- 첫 WSL 설정 시 **관리자 권한**
- **RAM 8GB 이상** (7B 모델 동시 실행 시 16GB 권장)
- 인터넷 (pip / ollama pull / Telegram outbound)
- **NVIDIA GPU 선택** — 권장 모델 `gemma4:latest`은 CPU로도 충분

### 2. WSL2 + Ubuntu 설치

**관리자 PowerShell**에서:

```powershell
wsl --install -d Ubuntu-22.04
```

재부팅 후 시작 메뉴에서 "Ubuntu 22.04" 실행, username/password 설정.
이후 모든 명령은 **Ubuntu 셸**에서.

WSL2 확인:

```powershell
wsl -l -v
```

`VERSION`이 `2`여야 함. `1`이면:

```powershell
wsl --set-version Ubuntu-22.04 2
```

> **WSL1은 미지원** — `/proc/self/fd`와 전체 시그널 시맨틱이 부재. 항상 WSL2.

### 3. Python 3.11+ 설치

Ubuntu 22.04는 Python 3.10이라 deadsnakes PPA 사용:

```bash
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev build-essential git

python3.12 --version    # → Python 3.12.x
```

> Ubuntu 24.04은 Python 3.12 기본 — PPA 단계 건너뛰기.

### 4. Ollama 설치

#### A안 (권장): WSL2 안에 설치

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull gemma4:latest
curl -s http://127.0.0.1:11434/api/tags | head
```

#### B안: Windows 호스트에 설치

[Windows용 Ollama](https://ollama.com/download/windows) 설치. WSL2에서
Windows의 `127.0.0.1`은 직접 접근 불가 → Windows 호스트 IP 필요:

```bash
ip route show default | awk '{print $3}'
# 예: 172.28.176.1

sampyclaw gateway start \
  --provider local --model gemma4:latest \
  --base-url http://172.28.176.1:11434/v1
```

또는 Windows에서 `OLLAMA_HOST=0.0.0.0` 설정.

> **WSL2 mirrored 네트워킹** (Win11 23H2+ `.wslconfig`
> `networkingMode=mirrored`) 활성화 시 `127.0.0.1`이 양방향 — `--base-url`
> 불필요.

### 5. GPU 가속 (선택)

NVIDIA:
1. Windows에 최신 **NVIDIA Game Ready / Studio 드라이버** 설치 (WSL 안에 Linux 드라이버 X)
2. WSL2가 자동 감지: `nvidia-smi`로 확인
3. `ollama serve` 로그에 `CUDA driver detected` 표시

AMD/Intel은 Ollama GPU 호환표 참고 — WSL 커버리지 부분적.

### 6. sampyClaw 설치

**Linux 홈 디렉토리**에 클론 (`/mnt/c/...` 아래 X — git/pip가 10–100배 느림):

```bash
cd ~
git clone https://github.com/andreason21/sampyClaw.git
cd sampyClaw

python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"

sampyclaw paths
sampyclaw config validate

pytest -q
# 1035 passed, 10 skipped
```

### 7. 설정 + 실행

`~/.sampyclaw/config.yaml`:

```yaml
channels: {}
agents:
  default:
    provider: local
    model: gemma4:latest
    system_prompt: |
      You are a helpful assistant.
```

```bash
export SAMPYCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)
sampyclaw gateway start --provider local
```

`gateway listening on http://127.0.0.1:7331` 확인. **Windows 브라우저**에서
`http://127.0.0.1:7331/` 접속 — WSL2가 localhost를 자동 포워딩.

### 8. (선택) LAN 접근

다른 PC에서 접근하려면 `0.0.0.0` 바인드:

```bash
sampyclaw gateway start --host 0.0.0.0 --port 7331 --provider local
```

Windows 방화벽이 막으면 7331 포트 열기. WSL2가 LAN 자동 포워딩.

### 9. (선택) systemd 서비스

WSL 0.67.6+에서 systemd 지원. `/etc/wsl.conf`:

```ini
[boot]
systemd=true
```

PowerShell에서 `wsl --shutdown` → 재시작 →
[`docs/OPERATIONS.md`](OPERATIONS.md)의 systemd 유닛 사용 가능.

systemd 없으면 `nohup`:

```bash
nohup sampyclaw gateway start --provider local > ~/sampyclaw.log 2>&1 &
disown
```

### 10. Telegram on WSL2

Telegram은 outbound-only — 게이트웨이가 Telegram 서버에 long-polling.
포트 포워딩, 공인 IP, 방화벽 설정 모두 불필요. README와 동일하게
`~/.sampyclaw/credentials/telegram/main.json` 작성 + `config.yaml`에 바인딩만 추가.

### 11. 자주 겪는 문제

| 증상 | 원인 / 해결 |
|---|---|
| pip install이 느리고 디스크 에러 | `/mnt/c/...`에 클론함. `~`로 이동. WSL2의 NTFS 브릿지가 ext4보다 훨씬 느림 |
| 게이트웨이에서 `127.0.0.1:11434` 접근 거부 | Ollama가 Windows 호스트에 있음. 위 B안 (Windows 호스트 IP) 또는 WSL2에 Ollama 설치 |
| Windows 브라우저에서 `localhost:7331` 못 열림 | `127.0.0.1` 또는 `0.0.0.0` 바인드 확인. `wsl --shutdown` 후 재실행 (포워더 stuck 가능) |
| pyright/pre-commit CRLF 에러 | WSL2에서 `git config --global core.autocrlf input` |
| `git commit` GPG 에러 | WSL에 Windows GPG agent 없음. `git config --local commit.gpgsign false` 또는 Ubuntu 안에 gpg 셋업 |
| Ollama 응답 느림 | Windows 절전 모드가 WSL2 throttle. Windows 전원 옵션을 "고성능"으로 |
| `ollama serve`가 "Address already in use" | Windows 호스트 Ollama가 11434 점유. 작업 관리자로 종료 또는 WSL Ollama를 `OLLAMA_HOST=127.0.0.1:11435 ollama serve`로 다른 포트에 |

### 12. 동작 검증

```bash
curl -s http://127.0.0.1:11434/api/tags | head            # Ollama 응답
curl -s http://127.0.0.1:7331/healthz                      # 게이트웨이 alive
curl -s http://127.0.0.1:7331/readyz                       # readiness OK/degraded
curl -s http://127.0.0.1:7331/metrics | head               # 메트릭 노출
sampyclaw message send --agent default "say hi"            # E2E RPC
```

5개 모두 성공하면 WSL2에서 프로덕션 가능 상태.
