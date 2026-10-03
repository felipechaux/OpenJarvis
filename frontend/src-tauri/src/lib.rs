use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tauri::menu::{MenuBuilder, MenuItemBuilder};
use tauri::tray::TrayIconBuilder;
use tauri::Manager;
use tauri_plugin_autostart::MacosLauncher;
use tokio::sync::Mutex;

const OLLAMA_PORT: u16 = 11434;
const JARVIS_PORT: u16 = 8000;

/// Small, fast model pulled at startup so the app opens quickly.
const STARTUP_MODEL: &str = "qwen3.5:4b";

/// Tiny fallback model if even the startup model can't be pulled.
const FALLBACK_MODEL: &str = "qwen3:0.6b";

/// Qwen3.5 model variants, ordered smallest to largest.
/// Each entry is (ollama_tag, approximate_download_size_gb, min_ram_gb).
const QWEN35_MODELS: &[(&str, f64, f64)] = &[
    ("qwen3.5:0.8b", 1.0, 4.0),
    ("qwen3.5:2b", 2.7, 6.0),
    ("qwen3.5:4b", 3.4, 8.0),
    ("qwen3.5:9b", 6.6, 12.0),
    ("qwen3.5:27b", 17.0, 24.0),
    ("qwen3.5:35b", 24.0, 32.0),
    ("qwen3.5:122b", 81.0, 96.0),
];

/// Get total system RAM in GB.
fn total_ram_gb() -> f64 {
    #[cfg(target_os = "macos")]
    {
        use std::process::Command;
        if let Ok(output) = Command::new("sysctl").args(["-n", "hw.memsize"]).output() {
            if let Ok(s) = String::from_utf8(output.stdout) {
                if let Ok(bytes) = s.trim().parse::<u64>() {
                    return bytes as f64 / (1024.0 * 1024.0 * 1024.0);
                }
            }
        }
    }
    #[cfg(target_os = "linux")]
    {
        if let Ok(contents) = std::fs::read_to_string("/proc/meminfo") {
            for line in contents.lines() {
                if line.starts_with("MemTotal:") {
                    if let Some(kb_str) = line.split_whitespace().nth(1) {
                        if let Ok(kb) = kb_str.parse::<u64>() {
                            return kb as f64 / (1024.0 * 1024.0);
                        }
                    }
                }
            }
        }
    }
    #[cfg(target_os = "windows")]
    {
        use std::process::Command;
        // wmic returns TotalVisibleMemorySize in KB
        if let Ok(output) = Command::new("wmic")
            .args(["OS", "get", "TotalVisibleMemorySize", "/value"])
            .output()
        {
            if let Ok(s) = String::from_utf8(output.stdout) {
                for line in s.lines() {
                    if let Some(val) = line.strip_prefix("TotalVisibleMemorySize=") {
                        if let Ok(kb) = val.trim().parse::<u64>() {
                            return kb as f64 / (1024.0 * 1024.0);
                        }
                    }
                }
            }
        }
    }
    8.0
}

/// Return the list of Qwen3.5 models that fit on this machine, smallest first.
fn models_that_fit() -> Vec<&'static str> {
    let ram = total_ram_gb();
    QWEN35_MODELS
        .iter()
        .filter(|(_, _, min_ram)| ram >= *min_ram)
        .map(|(tag, _, _)| *tag)
        .collect()
}

/// Pick the default model — prefers STARTUP_MODEL if it fits, otherwise
/// falls back to the third-largest model that fits on this machine.
fn preferred_model() -> &'static str {
    let fitting = models_that_fit();
    // Prefer STARTUP_MODEL when it fits (fast, good quality)
    if fitting.contains(&STARTUP_MODEL) {
        return STARTUP_MODEL;
    }
    match fitting.len() {
        0 => FALLBACK_MODEL,
        1 => fitting[0],
        2 => fitting[0],
        n => fitting[n - 3], // third-largest
    }
}

/// Get the user home directory, handling both Unix (HOME) and Windows (USERPROFILE).
fn home_dir() -> String {
    std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_default()
}

/// Extract a TOML string value from the right-hand side of `key = "value"`.
///
/// Strips any inline `# comment` suffix outside the quoted region before
/// unquoting so values like `default_model = "openrouter/x" # comment` parse
/// as `openrouter/x` rather than the full line including the comment.
fn parse_toml_string_value(raw: &str) -> String {
    let mut in_string = false;
    let mut end = raw.len();
    for (i, c) in raw.char_indices() {
        if c == '"' {
            in_string = !in_string;
        } else if c == '#' && !in_string {
            end = i;
            break;
        }
    }
    raw[..end]
        .trim()
        .trim_matches('"')
        .trim_matches('\'')
        .to_string()
}

/// Read ``default_model`` from ``~/.openjarvis/config.toml``.
///
/// Empty string when the file or key is missing — callers fall back to the
/// Ollama startup model in that case.
fn model_from_config() -> String {
    read_toml_key("default_model")
}

/// Read ``default_agent`` from ``~/.openjarvis/config.toml``.
///
/// Defaults to ``"simple"`` so existing behaviour is preserved when no
/// override is set.  The desktop config typically sets ``native_react``.
fn agent_from_config() -> String {
    let v = read_toml_key("default_agent");
    if v.is_empty() {
        "simple".to_string()
    } else {
        v
    }
}

/// Scan ``~/.openjarvis/config.toml`` for ``key = "value"`` and return the
/// unquoted, comment-stripped value (empty string when missing).
fn read_toml_key(key: &str) -> String {
    let path = format!("{}/.openjarvis/config.toml", home_dir());
    let Ok(text) = std::fs::read_to_string(&path) else {
        return String::new();
    };
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with(key) {
            if let Some(val) = trimmed.split('=').nth(1) {
                let v = parse_toml_string_value(val);
                if !v.is_empty() {
                    return v;
                }
            }
        }
    }
    String::new()
}

/// Resolve full path to a binary by checking common locations.
/// macOS .app bundles don't inherit the shell PATH, so we probe manually.
fn resolve_bin(name: &str) -> String {
    let home = home_dir();

    #[cfg(not(target_os = "windows"))]
    let candidates = vec![
        format!("/opt/homebrew/bin/{name}"),
        format!("{home}/.local/bin/{name}"),
        format!("{home}/.cargo/bin/{name}"),
        format!("/usr/local/bin/{name}"),
        format!("/usr/bin/{name}"),
    ];

    #[cfg(target_os = "windows")]
    let candidates = {
        let localappdata = std::env::var("LOCALAPPDATA").unwrap_or_default();
        let programfiles = std::env::var("ProgramFiles").unwrap_or_default();
        let programfiles_x86 = std::env::var("ProgramFiles(x86)").unwrap_or_default();
        vec![
            // Git for Windows — standard install paths
            format!("{programfiles}\\Git\\cmd\\{name}.exe"),
            format!("{programfiles_x86}\\Git\\cmd\\{name}.exe"),
            format!("{localappdata}\\Programs\\Git\\cmd\\{name}.exe"),
            // Scoop package manager
            format!("{home}\\scoop\\shims\\{name}.exe"),
            // Cargo, local bin
            format!("{home}\\.cargo\\bin\\{name}.exe"),
            format!("{home}\\.local\\bin\\{name}.exe"),
            // Generic program locations
            format!("{localappdata}\\Programs\\{name}\\{name}.exe"),
            format!("{programfiles}\\{name}\\{name}.exe"),
            // Ollama installs to LOCALAPPDATA on Windows
            format!("{localappdata}\\Programs\\Ollama\\{name}.exe"),
            // uv installs via pip/pipx
            format!("{home}\\AppData\\Roaming\\Python\\Scripts\\{name}.exe"),
        ]
    };

    for path in &candidates {
        if std::path::Path::new(path).exists() {
            return path.clone();
        }
    }

    // Fallback: ask the OS to find it on PATH.
    // On Windows this uses `where.exe`, on Unix `which`.
    #[cfg(target_os = "windows")]
    {
        if let Ok(output) = std::process::Command::new("where")
            .arg(format!("{name}.exe"))
            .output()
        {
            if output.status.success() {
                let stdout = String::from_utf8_lossy(&output.stdout);
                if let Some(first_line) = stdout.lines().next() {
                    let p = first_line.trim();
                    if !p.is_empty() && std::path::Path::new(p).exists() {
                        return p.to_string();
                    }
                }
            }
        }
    }
    #[cfg(not(target_os = "windows"))]
    {
        if let Ok(output) = std::process::Command::new("which").arg(name).output() {
            if output.status.success() {
                let stdout = String::from_utf8_lossy(&output.stdout);
                if let Some(first_line) = stdout.lines().next() {
                    let p = first_line.trim();
                    if !p.is_empty() && std::path::Path::new(p).exists() {
                        return p.to_string();
                    }
                }
            }
        }
    }

    name.to_string()
}

/// Find the OpenJarvis project root (contains pyproject.toml).
/// Checks OPENJARVIS_ROOT, then ~/.openjarvis/project-root, walks up from
/// the executable, then probes common clone locations.
fn find_project_root() -> Option<std::path::PathBuf> {
    // 1. Explicit env var override
    if let Ok(root) = std::env::var("OPENJARVIS_ROOT") {
        let path = std::path::PathBuf::from(&root);
        if path.join("pyproject.toml").exists() {
            return Some(path);
        }
    }

    // 2. ~/.openjarvis/project-root: the installed app, launched from Finder
    //    or at login, gets no OPENJARVIS_ROOT; this file names the clone.
    let pointer = std::path::PathBuf::from(home_dir()).join(".openjarvis/project-root");
    if let Ok(text) = std::fs::read_to_string(&pointer) {
        let path = std::path::PathBuf::from(text.trim());
        if path.join("pyproject.toml").exists() {
            return Some(path);
        }
    }

    // 3. Walk up from the running executable (works in dev and .app bundle)
    if let Ok(exe) = std::env::current_exe() {
        let mut dir = exe.parent().map(|p| p.to_path_buf());
        for _ in 0..8 {
            if let Some(ref d) = dir {
                if d.join("pyproject.toml").exists() {
                    return Some(d.clone());
                }
                dir = d.parent().map(|p| p.to_path_buf());
            }
        }
    }

    // 4. Fallback: well-known direct paths
    let home = home_dir();
    let direct = [
        format!("{home}/OpenJarvis"),
        format!("{home}/projects/hazy/OpenJarvis"),
        format!("{home}/projects/OpenJarvis"),
        format!("{home}/src/OpenJarvis"),
        format!("{home}/Documents/OpenJarvis"),
        format!("{home}/Desktop/OpenJarvis"),
        format!("{home}/Developer/OpenJarvis"),
        format!("{home}/dev/OpenJarvis"),
        format!("{home}/Code/OpenJarvis"),
        format!("{home}/code/OpenJarvis"),
        format!("{home}/repos/OpenJarvis"),
        format!("{home}/github/OpenJarvis"),
    ];
    for p in &direct {
        let path = std::path::PathBuf::from(p);
        if path.join("pyproject.toml").exists() {
            return Some(path);
        }
    }

    // 5. Shallow scan: look for OpenJarvis one level inside common parent dirs.
    //    This catches clones like ~/Documents/my-stuff/OpenJarvis without
    //    needing to enumerate every possible intermediate folder.
    let scan_parents = [
        format!("{home}/Documents"),
        format!("{home}/Desktop"),
        format!("{home}/Developer"),
        format!("{home}/projects"),
        format!("{home}/repos"),
        format!("{home}/src"),
        format!("{home}/Code"),
        format!("{home}/code"),
        format!("{home}/dev"),
        format!("{home}/github"),
    ];
    for parent in &scan_parents {
        let parent_path = std::path::PathBuf::from(parent);
        if let Ok(entries) = std::fs::read_dir(&parent_path) {
            for entry in entries.flatten() {
                let candidate = entry.path().join("OpenJarvis");
                if candidate.join("pyproject.toml").exists() {
                    return Some(candidate);
                }
                // Also check if the entry itself is OpenJarvis (case-insensitive match)
                if let Some(name) = entry.file_name().to_str() {
                    if name.eq_ignore_ascii_case("openjarvis")
                        && entry.path().join("pyproject.toml").exists()
                    {
                        return Some(entry.path());
                    }
                }
            }
        }
    }

    None
}

// ---------------------------------------------------------------------------
// BackendManager — owns the Ollama + Jarvis server child processes
// ---------------------------------------------------------------------------

struct ChildHandle {
    child: tokio::process::Child,
}

impl ChildHandle {
    async fn kill(&mut self) {
        let _ = self.child.kill().await;
    }
}

#[derive(Default)]
struct BackendManager {
    ollama: Option<ChildHandle>,
    jarvis: Option<ChildHandle>,
    /// True once the JarvisWake sidecar has been launched via Launch
    /// Services.  We don't hold a ``ChildHandle`` for it because ``open
    /// -a`` returns immediately after kicking off the .app — the sidecar
    /// process itself is detached.  On shutdown we ``pkill`` it by name.
    wake_launched: bool,
}

impl BackendManager {
    async fn stop_all(&mut self) {
        if let Some(ref mut h) = self.jarvis {
            h.kill().await;
        }
        self.jarvis = None;
        if let Some(ref mut h) = self.ollama {
            h.kill().await;
        }
        self.ollama = None;
        if self.wake_launched {
            // The sidecar was launched detached (so TCC attributes the
            // privacy request to its .app bundle).  Reap it explicitly.
            let _ = tokio::process::Command::new("pkill")
                .args(["-f", "JarvisWake.app/Contents/MacOS/JarvisWake"])
                .status()
                .await;
            self.wake_launched = false;
        }
    }
}

type SharedBackend = Arc<Mutex<BackendManager>>;

// ---------------------------------------------------------------------------
// Setup status (reported to frontend)
// ---------------------------------------------------------------------------

#[derive(serde::Serialize, Clone)]
struct SetupStatus {
    phase: String,
    detail: String,
    ollama_ready: bool,
    server_ready: bool,
    model_ready: bool,
    error: Option<String>,
    /// Installed CLIs offered on the setup screen while ``phase`` is
    /// ``"choose_engine"``.
    cli_options: Vec<CliOption>,
}

impl Default for SetupStatus {
    fn default() -> Self {
        Self {
            phase: "starting".into(),
            detail: "Initializing...".into(),
            ollama_ready: false,
            server_ready: false,
            model_ready: false,
            error: None,
            cli_options: Vec::new(),
        }
    }
}

type SharedStatus = Arc<Mutex<SetupStatus>>;

// ---------------------------------------------------------------------------
// Health-check helpers
// ---------------------------------------------------------------------------

async fn wait_for_url(url: &str, timeout: Duration) -> bool {
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap();
    let deadline = tokio::time::Instant::now() + timeout;
    while tokio::time::Instant::now() < deadline {
        if let Ok(resp) = client.get(url).send().await {
            if resp.status().is_success() {
                return true;
            }
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
    false
}

async fn ollama_has_model(model: &str) -> bool {
    let url = format!("http://127.0.0.1:{}/api/tags", OLLAMA_PORT);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .unwrap();
    if let Ok(resp) = client.get(&url).send().await {
        if let Ok(body) = resp.json::<serde_json::Value>().await {
            if let Some(models) = body.get("models").and_then(|m| m.as_array()) {
                return models.iter().any(|m| {
                    m.get("name")
                        .and_then(|n| n.as_str())
                        .map(|n| {
                            n == model
                                || n.strip_suffix(":latest") == Some(model)
                                || model.strip_suffix(":latest") == Some(n)
                        })
                        .unwrap_or(false)
                });
            }
        }
    }
    false
}

async fn pull_model(model: &str) -> Result<(), String> {
    let url = format!("http://127.0.0.1:{}/api/pull", OLLAMA_PORT);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(600))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .post(&url)
        .json(&serde_json::json!({"name": model, "stream": false}))
        .send()
        .await
        .map_err(|e| format!("Pull request failed: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!("Pull returned status {}", resp.status()));
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Backend boot sequence (runs in background after app launch)
// ---------------------------------------------------------------------------

/// Return ``true`` when the configured ``default_model`` points at a cloud
/// provider rather than Ollama, so the boot path can skip the local engine
/// entirely (no startup model download, no ollama serve sidecar).
fn is_cloud_model(model: &str) -> bool {
    let m = model.to_ascii_lowercase();
    // Provider-prefixed (openrouter/, openai/, anthropic/) always have a slash.
    if m.contains('/') {
        return true;
    }
    // Well-known cloud model name patterns without a slash prefix.
    m.starts_with("gpt-")
        || m.starts_with("claude-")
        || m.starts_with("o1-")
        || m.starts_with("o3-")
        || m.starts_with("gemini-")
        || m.starts_with("chatgpt-")
}

// ---------------------------------------------------------------------------
// First-run engine choice: coding-agent CLIs on the user's subscription
// ---------------------------------------------------------------------------

/// A coding-agent CLI installed on this machine that JARVIS can run on.
#[derive(serde::Serialize, Clone)]
struct CliOption {
    key: String,
    label: String,
    model: String,
}

/// Engine key, display name, binary, default model — mirrors
/// ``CLI_ENGINE_MODELS`` in ``openjarvis/core/config.py``.
const CLI_ENGINES: &[(&str, &str, &str, &str)] = &[
    ("claude_cli", "Claude Code", "claude", "claude-cli/sonnet"),
    ("kiro_cli", "Kiro", "kiro-cli", "kiro-cli/auto"),
    ("antigravity_cli", "Antigravity", "agy", "antigravity/default"),
    ("gemini_cli", "Gemini CLI", "gemini", "gemini-cli/default"),
];

fn detect_cli_engines() -> Vec<CliOption> {
    CLI_ENGINES
        .iter()
        // ``resolve_bin`` hands back the bare name when nothing is found.
        .filter(|(_, _, bin, _)| std::path::Path::new(&resolve_bin(bin)).is_absolute())
        .map(|(key, label, _, model)| CliOption {
            key: key.to_string(),
            label: label.to_string(),
            model: model.to_string(),
        })
        .collect()
}

/// Engine picked on the setup screen: a CLI engine key or ``"ollama"``.
static ENGINE_CHOICE: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);

#[tauri::command]
fn choose_engine(choice: String) {
    *ENGINE_CHOICE.lock().unwrap() = Some(choice);
}

async fn wait_for_engine_choice() -> String {
    loop {
        let choice = ENGINE_CHOICE.lock().unwrap().take();
        if let Some(choice) = choice {
            return choice;
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
}

/// Persist the chosen CLI via ``jarvis config use-cli``: engine, model and
/// the JARVIS agent with its tools (keeps the rest of the config intact).
async fn save_engine_choice(opt: &CliOption) -> Result<(), String> {
    let root = find_project_root()
        .ok_or_else(|| "Could not find the OpenJarvis repo to save the choice.".to_string())?;
    let out = tokio::process::Command::new(resolve_bin("uv"))
        .args(["run", "jarvis", "config", "use-cli", &opt.key])
        .current_dir(&root)
        .output()
        .await
        .map_err(|e| format!("Could not save the engine choice: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "Could not save the engine choice: {}",
            String::from_utf8_lossy(&out.stderr).trim()
        ));
    }
    Ok(())
}

async fn boot_backend(backend: SharedBackend, status: SharedStatus) {
    let mut resolved_model = model_from_config();

    // No model configured yet: offer the installed coding-agent CLIs before
    // falling back to downloading a local Ollama model.
    if resolved_model.is_empty() {
        let options = detect_cli_engines();
        if !options.is_empty() {
            {
                let mut s = status.lock().await;
                s.phase = "choose_engine".into();
                s.detail = "Choose how JARVIS should think.".into();
                s.cli_options = options.clone();
            }
            let choice = wait_for_engine_choice().await;
            {
                let mut s = status.lock().await;
                s.cli_options.clear();
                s.phase = "starting".into();
                s.detail = "Saving your choice...".into();
            }
            if let Some(opt) = options.iter().find(|o| o.key == choice) {
                if let Err(e) = save_engine_choice(opt).await {
                    status.lock().await.error = Some(e);
                    return;
                }
                resolved_model = opt.model.clone();
            }
        }
    }
    let cloud_only = is_cloud_model(&resolved_model);

    if cloud_only {
        // Cloud model — skip Ollama bootstrap entirely.  Downloading a 3+ GB
        // Qwen weight just to ignore it (because chat traffic flows to
        // OpenRouter / OpenAI / Anthropic / Gemini) wastes bandwidth, disk,
        // and minutes on every cold start.
        let mut s = status.lock().await;
        s.phase = "server".into();
        s.detail = format!("Using cloud model {resolved_model}; skipping Ollama.");
        s.ollama_ready = true;
        s.model_ready = true;
    } else {
        // Phase 1: Start Ollama
        {
            let mut s = status.lock().await;
            s.phase = "ollama".into();
            s.detail = "Starting inference engine...".into();
        }

        // Try the bundled sidecar first, fall back to system ollama
        let ollama_child = {
            let ollama_bin = resolve_bin("ollama");
            let sidecar = tokio::process::Command::new(&ollama_bin)
                .arg("serve")
                .env("OLLAMA_HOST", format!("127.0.0.1:{}", OLLAMA_PORT))
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .spawn();
            match sidecar {
                Ok(child) => Some(child),
                Err(_) => None,
            }
        };

        if let Some(child) = ollama_child {
            backend.lock().await.ollama = Some(ChildHandle { child });
        }

        let ollama_url = format!("http://127.0.0.1:{}/api/tags", OLLAMA_PORT);
        let ollama_ok = wait_for_url(&ollama_url, Duration::from_secs(30)).await;

        if !ollama_ok {
            let mut s = status.lock().await;
            s.error = Some("Could not start Ollama. Install it from https://ollama.com".into());
            return;
        }

        {
            let mut s = status.lock().await;
            s.ollama_ready = true;
            s.detail = "Inference engine ready.".into();
        }

        // Phase 2: Pull one small model (qwen3.5:2b) so the app can open fast.
        // Remaining models are pulled in the background after the server starts.
        {
            let mut s = status.lock().await;
            s.phase = "model".into();
            s.detail = format!("Checking for {}...", STARTUP_MODEL);
        }

        if !ollama_has_model(STARTUP_MODEL).await {
            {
                let mut s = status.lock().await;
                s.detail = format!("Downloading {}... (this may take a minute)", STARTUP_MODEL);
            }
            if let Err(e) = pull_model(STARTUP_MODEL).await {
                // If the startup model fails, try the tiny fallback
                eprintln!("Warning: failed to pull {}: {}", STARTUP_MODEL, e);
                if !ollama_has_model(FALLBACK_MODEL).await {
                    let mut s = status.lock().await;
                    s.detail = format!("Downloading {}...", FALLBACK_MODEL);
                    drop(s);
                    if let Err(e2) = pull_model(FALLBACK_MODEL).await {
                        let mut s = status.lock().await;
                        s.error = Some(format!("Failed to download model: {}", e2));
                        return;
                    }
                }
            }
        }

        {
            let mut s = status.lock().await;
            s.model_ready = true;
            s.detail = "Model ready.".into();
        }
    }

    // Phase 3: Start jarvis serve
    {
        let mut s = status.lock().await;
        s.phase = "server".into();
        s.detail = "Starting API server...".into();
    }

    let uv_bin = resolve_bin("uv");

    // Verify uv is actually installed
    if !std::path::Path::new(&uv_bin).exists() && uv_bin == "uv" {
        let mut s = status.lock().await;
        s.error = Some(
            "Could not find 'uv' (Python package manager). \
             Install it from https://astral.sh/uv then relaunch."
                .into(),
        );
        return;
    }

    let mut project_root = find_project_root();

    if project_root.is_none() {
        // Auto-clone on first launch
        let git_bin = resolve_bin("git");

        // Check that git is installed
        if !std::path::Path::new(&git_bin).exists() && git_bin == "git" {
            let mut s = status.lock().await;
            s.error = Some(
                "Could not find 'git'. \
                 Install it from https://git-scm.com then relaunch."
                    .into(),
            );
            return;
        }

        let target_path = std::path::PathBuf::from(home_dir()).join("OpenJarvis");
        let clone_target = target_path.display().to_string();

        // If the directory exists but is not a valid project, don't overwrite
        if target_path.exists() && !target_path.join("pyproject.toml").exists() {
            let mut s = status.lock().await;
            s.error = Some(format!(
                "{} exists but is not a valid OpenJarvis project. \
                 Remove it and relaunch, or set OPENJARVIS_ROOT to the correct path.",
                clone_target,
            ));
            return;
        }

        {
            let mut s = status.lock().await;
            s.detail = "Downloading OpenJarvis (first launch)...".into();
        }

        let clone_result = tokio::process::Command::new(&git_bin)
            .args([
                "clone",
                "--depth",
                "1",
                "https://github.com/open-jarvis/OpenJarvis.git",
                &clone_target,
            ])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::piped())
            .spawn();

        match clone_result {
            Ok(child) => match child.wait_with_output().await {
                Ok(output) if output.status.success() => {
                    project_root = Some(target_path);
                }
                Ok(output) => {
                    let stderr = String::from_utf8_lossy(&output.stderr);
                    let mut s = status.lock().await;
                    s.error = Some(format!(
                        "Failed to download OpenJarvis: {}. \
                         Clone manually: git clone https://github.com/open-jarvis/OpenJarvis.git {}",
                        stderr.trim(),
                        clone_target,
                    ));
                    return;
                }
                Err(e) => {
                    let mut s = status.lock().await;
                    s.error = Some(format!(
                        "Failed to download OpenJarvis: {}. \
                         Clone manually: git clone https://github.com/open-jarvis/OpenJarvis.git {}",
                        e, clone_target,
                    ));
                    return;
                }
            },
            Err(e) => {
                let mut s = status.lock().await;
                s.error = Some(format!(
                    "Could not run git: {}. \
                     Install git from https://git-scm.com then relaunch.",
                    e,
                ));
                return;
            }
        }
    }

    // Kill any leftover server on our port from a previous run
    {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .unwrap();
        if client
            .get(format!("http://127.0.0.1:{}/health", JARVIS_PORT))
            .send()
            .await
            .is_ok()
        {
            // Something is already listening — try to kill it
            #[cfg(target_os = "macos")]
            {
                // Only the listening server: plain `-i :port` also lists
                // clients of the port — this app included — and kill -9'd it.
                if let Ok(output) = tokio::process::Command::new("lsof")
                    .args(["-t", "-sTCP:LISTEN", "-i", &format!("tcp:{}", JARVIS_PORT)])
                    .output()
                    .await
                {
                    let pids = String::from_utf8_lossy(&output.stdout);
                    for pid in pids.lines() {
                        if let Ok(pid_val) = pid.trim().parse::<i32>() {
                            let _ = tokio::process::Command::new("kill")
                                .args(["-9", &pid_val.to_string()])
                                .output()
                                .await;
                        }
                    }
                }
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
            #[cfg(all(unix, not(target_os = "macos")))]
            {
                let _ = tokio::process::Command::new("fuser")
                    .args(["-k", &format!("{}/tcp", JARVIS_PORT)])
                    .output()
                    .await;
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
            #[cfg(target_os = "windows")]
            {
                // Find the PID holding the port via netstat, then kill it
                if let Ok(output) = tokio::process::Command::new("cmd")
                    .args(["/C", &format!(
                        "for /f \"tokens=5\" %a in ('netstat -ano ^| findstr :{port} ^| findstr LISTENING') do taskkill /PID %a /F",
                        port = JARVIS_PORT,
                    )])
                    .output()
                    .await
                {
                    let _ = output; // best-effort
                }
                tokio::time::sleep(Duration::from_secs(2)).await;
            }
        }
    }

    // When the configured ``default_model`` is a cloud model, forward it
    // verbatim to ``jarvis serve --model`` so chat traffic flows to OpenRouter
    // / OpenAI / Anthropic / Gemini.  Otherwise probe Ollama for an available
    // local model.
    let startup_model: String = if cloud_only {
        resolved_model.clone()
    } else {
        let pref = preferred_model();
        if ollama_has_model(pref).await {
            pref.to_string()
        } else if ollama_has_model(STARTUP_MODEL).await {
            STARTUP_MODEL.to_string()
        } else {
            FALLBACK_MODEL.to_string()
        }
    };

    let root = project_root.as_ref().unwrap();

    // Install dependencies automatically (handles fresh clones)
    {
        let mut s = status.lock().await;
        s.detail = "Installing dependencies...".into();
    }
    let _ = tokio::process::Command::new(&uv_bin)
        .args([
            "sync",
            "--extra", "server",
            "--extra", "inference-cloud",
            "--extra", "inference-google",
            // Speech (faster-whisper) — without this the STT backend
            // imports faster_whisper as None and /v1/speech/health reports
            // ``available: false``, leaving the UI stuck on "Backend not
            // configured" even with the rest of the wake-word stack wired.
            "--extra", "speech",
        ])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .current_dir(root)
        .status()
        .await;

    {
        let mut s = status.lock().await;
        s.detail = format!(
            "Starting server with {} from {}...",
            startup_model,
            root.display(),
        );
    }

    let resolved_agent = agent_from_config();
    let mut cmd = tokio::process::Command::new(&uv_bin);
    cmd.args([
        "run",
        "jarvis",
        "serve",
        // Bind loopback only.  Without ``--host`` the auth middleware
        // requires ``OPENJARVIS_API_KEY`` (because it would otherwise expose
        // the API on 0.0.0.0).  The desktop app is a local-only client, so
        // 127.0.0.1 is both safer and avoids the auth gate.
        "--host",
        "127.0.0.1",
        "--port",
        &JARVIS_PORT.to_string(),
        "--model",
        &startup_model,
        "--agent",
        &resolved_agent,
    ])
    .stdout(std::process::Stdio::null())
    .stderr(std::process::Stdio::piped())
    .current_dir(root);

    // Inject cloud API keys from ~/.openjarvis/cloud-keys.env
    for (key, value) in read_cloud_keys() {
        cmd.env(&key, &value);
    }
    let jarvis_child = cmd.spawn();

    match jarvis_child {
        Ok(child) => {
            backend.lock().await.jarvis = Some(ChildHandle { child });
        }
        Err(e) => {
            let mut s = status.lock().await;
            s.error = Some(format!(
                "Could not start jarvis server: {}. \
                 Make sure uv is installed (https://astral.sh/uv) and the OpenJarvis repo is cloned at {}",
                e,
                root.display(),
            ));
            return;
        }
    }

    let server_url = format!("http://127.0.0.1:{}/health", JARVIS_PORT);
    let server_ok = wait_for_url(&server_url, Duration::from_secs(600)).await;

    if !server_ok {
        // Try to read stderr from the failed process for a useful error
        let mut stderr_msg = String::new();
        {
            let mut mgr = backend.lock().await;
            if let Some(ref mut h) = mgr.jarvis {
                if let Some(ref mut stderr) = h.child.stderr.take() {
                    use tokio::io::AsyncReadExt;
                    let mut buf = vec![0u8; 4096];
                    if let Ok(n) = stderr.read(&mut buf).await {
                        stderr_msg = String::from_utf8_lossy(&buf[..n]).to_string();
                    }
                }
            }
        }
        let detail = if stderr_msg.is_empty() {
            format!(
                "Jarvis server did not start. Check that:\n\
                 1. uv is installed ({})\n\
                 2. The OpenJarvis repo is at {}\n\
                 3. Run 'uv sync' in that directory",
                uv_bin,
                root.display(),
            )
        } else {
            format!("Server failed to start: {}", stderr_msg.trim())
        };
        let mut s = status.lock().await;
        s.error = Some(detail);
        return;
    }

    // Drain the server's stderr into a log file.  Nothing read the pipe
    // after startup, so once its ~64 KB buffer filled (warnings, logging),
    // every write in the server blocked and requests hung — JARVIS
    // "sometimes didn't answer".
    {
        let mut mgr = backend.lock().await;
        if let Some(stderr) = mgr.jarvis.as_mut().and_then(|h| h.child.stderr.take()) {
            tokio::spawn(drain_server_stderr(stderr));
        }
    }

    {
        let mut s = status.lock().await;
        s.server_ready = true;
        s.phase = "ready".into();
        s.detail = "All systems ready.".into();
    }

    // Phase 4: Pull remaining Qwen3.5 models in the background — only when
    // running against a local Ollama engine.  Cloud-model setups never use
    // these weights so downloading them would just burn disk.
    if !cloud_only {
        let fitting = models_that_fit();
        tokio::spawn(async move {
            for model in fitting {
                if model != STARTUP_MODEL && model != FALLBACK_MODEL {
                    if !ollama_has_model(model).await {
                        let _ = pull_model(model).await;
                    }
                }
            }
        });
    }
}

/// Server log written by the desktop app (replaced on every launch).
fn server_log_path() -> std::path::PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    std::path::Path::new(&home).join(".openjarvis/logs/desktop-server.log")
}

/// Copy the server's stderr to [`server_log_path`] until it exits, keeping
/// the pipe empty.  If the file cannot be opened the output is discarded,
/// which still keeps the server from blocking.
async fn drain_server_stderr(mut stderr: tokio::process::ChildStderr) {
    use tokio::io::AsyncWriteExt;
    let path = server_log_path();
    if let Some(dir) = path.parent() {
        let _ = tokio::fs::create_dir_all(dir).await;
    }
    match tokio::fs::File::create(&path).await {
        Ok(mut file) => {
            let _ = tokio::io::copy(&mut stderr, &mut file).await;
            let _ = file.flush().await;
        }
        Err(_) => {
            let _ = tokio::io::copy(&mut stderr, &mut tokio::io::sink()).await;
        }
    }
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

fn api_base() -> String {
    format!("http://127.0.0.1:{}", JARVIS_PORT)
}

#[tauri::command]
async fn get_setup_status(state: tauri::State<'_, SharedStatus>) -> Result<SetupStatus, String> {
    Ok(state.lock().await.clone())
}

#[tauri::command]
fn get_api_base() -> String {
    api_base()
}

/// True once the JarvisWake sidecar has reported ``ready``.  The
/// ``jarvis-wake-ready`` event fires ~1s after launch, long before the
/// webview subscribes (it waits for the Python backend first), so the
/// frontend polls this instead of relying on catching the event.
static WAKE_READY: AtomicBool = AtomicBool::new(false);

#[tauri::command]
fn wake_ready() -> bool {
    WAKE_READY.load(Ordering::SeqCst)
}

/// Path to the bundled JarvisWake Swift sidecar executable.
///
/// The sidecar lives inside ``JarvisWake.app/Contents/MacOS/JarvisWake``
/// (rather than as a bare binary) because macOS TCC only reads
/// ``NSMicrophoneUsageDescription`` / ``NSSpeechRecognitionUsageDescription``
/// from a real ``.app`` bundle's adjacent Info.plist — embedding the plist
/// in the binary's ``__TEXT,__info_plist`` section crashes with a missing-
/// usage-description error at first speech-framework call.
///
/// We probe the packaged Tauri bundle first so production runs never
/// accidentally fall through to the dev workspace path.
#[cfg(target_os = "macos")]
fn wake_binary_path() -> Option<std::path::PathBuf> {
    const REL_BUNDLE: &str = "JarvisWake.app/Contents/MacOS/JarvisWake";

    // 1. Packaged Tauri .app: ``Contents/MacOS/JarvisWake.app`` or
    //    ``Contents/Resources/binaries/JarvisWake.app`` (depending on layout).
    if let Ok(exe) = std::env::current_exe() {
        if let Some(macos_dir) = exe.parent() {
            let candidates: Vec<std::path::PathBuf> = [
                Some(macos_dir.join(REL_BUNDLE)),
                macos_dir
                    .parent()
                    .map(|p| p.join("Resources/binaries").join(REL_BUNDLE)),
            ]
            .into_iter()
            .flatten()
            .collect();
            for candidate in candidates {
                if candidate.exists() {
                    return Some(candidate);
                }
            }
        }
    }
    // 2. Dev: frontend/src-tauri/binaries/JarvisWake.app
    if let Some(root) = find_project_root() {
        let p = root
            .join("frontend/src-tauri/binaries")
            .join(REL_BUNDLE);
        if p.exists() {
            return Some(p);
        }
    }
    // 3. CARGO_MANIFEST_DIR fallback (cargo run from src-tauri/).
    let manifest = env!("CARGO_MANIFEST_DIR");
    let p = std::path::PathBuf::from(manifest)
        .join("binaries")
        .join(REL_BUNDLE);
    if p.exists() {
        return Some(p);
    }
    None
}

/// Spawn the JarvisWake Swift binary and forward its ``wake`` events to
/// the webview via Tauri's event system.  The frontend subscribes to
/// ``"jarvis-wake"`` and reacts (e.g. focuses the window, starts the mic).
#[cfg(target_os = "macos")]
async fn spawn_wake_listener(app: tauri::AppHandle, backend: SharedBackend) {
    use tauri::Emitter;
    use tokio::io::{AsyncReadExt, AsyncSeekExt};

    // Idempotency guard: the setup hook spawns this once on launch, and the
    // ``start_backend`` command can also spawn it.  Don't double up.
    if backend.lock().await.wake_launched {
        return;
    }

    let bin = match wake_binary_path() {
        Some(p) => p,
        None => {
            eprintln!("[wake] jarvis-wake binary not found; wake-word disabled");
            let _ = app.emit("jarvis-wake-error", "sidecar binary not found");
            return;
        }
    };

    // Walk up from ``.../JarvisWake.app/Contents/MacOS/JarvisWake`` to the
    // ``JarvisWake.app`` bundle directory.  Launching the binary directly
    // works fine but TCC then attributes the Speech / Microphone privacy
    // request to the *parent* (our Tauri dev binary, which has no usage
    // descriptions) and aborts the child with SIGABRT.  ``open -a`` goes
    // through Launch Services so TCC attributes the request to the
    // sidecar's own .app bundle and reads its Info.plist properly.
    let bundle = match bin.ancestors().nth(3) {
        Some(p) => p.to_path_buf(),
        None => {
            eprintln!("[wake] cannot derive bundle path from {}", bin.display());
            let _ = app.emit("jarvis-wake-error", "bundle path lookup failed");
            return;
        }
    };

    // Redirect the sidecar's stdout to a temp log file we can tail.  ``open``
    // doesn't pipe stdout back to its caller, so we use its ``--stdout``
    // flag (macOS 14+) to point /dev/stdout at a path.
    let log_path = std::env::temp_dir().join(format!(
        "jarvis-wake-{}.log",
        std::process::id()
    ));
    // Ensure the file exists and is empty so our reader starts fresh
    let _ = std::fs::remove_file(&log_path);
    if let Err(e) = std::fs::File::create(&log_path) {
        eprintln!("[wake] cannot create log file {}: {}", log_path.display(), e);
        let _ = app.emit("jarvis-wake-error", "log create failed");
        return;
    }

    let status = tokio::process::Command::new("open")
        .arg("-a")
        .arg(&bundle)
        .arg("--stdout")
        .arg(&log_path)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .await;
    match status {
        Ok(s) if s.success() => {}
        Ok(s) => {
            eprintln!("[wake] `open -a` exited with status {}", s);
            let _ = app.emit("jarvis-wake-error", "launch services failed");
            return;
        }
        Err(e) => {
            eprintln!("[wake] failed to invoke `open`: {}", e);
            let _ = app.emit("jarvis-wake-error", "open command failed");
            return;
        }
    }

    backend.lock().await.wake_launched = true;
    eprintln!("[wake] sidecar launched, tailing {}", log_path.display());

    // Tail the log file: reopen + seek-to-last-pos each tick so we always
    // see freshly-appended bytes.  Tokio's ``File`` caches EOF state after
    // ``read_to_string`` returns 0, which breaks the more obvious "open
    // once, read in a loop" pattern.  Reopening dodges the issue at the
    // cost of one extra syscall per 150 ms — negligible.
    let mut last_pos: u64 = 0;
    let mut leftover = String::new();
    loop {
        let bytes = match tokio::fs::read(&log_path).await {
            Ok(b) => b,
            Err(e) => {
                eprintln!("[wake] log read error: {}", e);
                tokio::time::sleep(Duration::from_millis(500)).await;
                continue;
            }
        };
        let size = bytes.len() as u64;
        if size < last_pos {
            // File got truncated (rare — usually means the sidecar
            // was restarted out-of-band).  Reset and re-read from 0.
            last_pos = 0;
            leftover.clear();
        }
        if size > last_pos {
            let new_slice = &bytes[last_pos as usize..];
            last_pos = size;
            if let Ok(s) = std::str::from_utf8(new_slice) {
                leftover.push_str(s);
                while let Some(i) = leftover.find('\n') {
                    let line: String = leftover.drain(..=i).collect();
                    let line = line.trim();
                    if line.is_empty() {
                        continue;
                    }
                    let parsed: serde_json::Value = match serde_json::from_str(line) {
                        Ok(v) => v,
                        Err(_) => continue,
                    };
                    let event = parsed.get("event").and_then(|v| v.as_str()).unwrap_or("");
                    match event {
                        "wake" => {
                            let text = parsed.get("text").and_then(|v| v.as_str()).unwrap_or("");
                            eprintln!("[wake] forwarding wake event to JS: {}", text);
                            let _ = app.emit("jarvis-wake", text);
                        }
                        "ready" => {
                            eprintln!("[wake] sidecar ready");
                            WAKE_READY.store(true, Ordering::SeqCst);
                            let _ = app.emit("jarvis-wake-ready", true);
                        }
                        "error" => {
                            let msg = parsed
                                .get("message")
                                .and_then(|v| v.as_str())
                                .unwrap_or("(unknown)");
                            eprintln!("[wake] sidecar error: {}", msg);
                            WAKE_READY.store(false, Ordering::SeqCst);
                            let _ = app.emit("jarvis-wake-error", msg);
                        }
                        _ => {}
                    }
                }
            }
        }
        tokio::time::sleep(Duration::from_millis(150)).await;
    }
}

#[tauri::command]
async fn start_backend(
    app: tauri::AppHandle,
    backend: tauri::State<'_, SharedBackend>,
    status: tauri::State<'_, SharedStatus>,
) -> Result<(), String> {
    let b = backend.inner().clone();
    let s = status.inner().clone();
    tauri::async_runtime::spawn(boot_backend(b.clone(), s));
    #[cfg(target_os = "macos")]
    tauri::async_runtime::spawn(spawn_wake_listener(app, b));
    #[cfg(not(target_os = "macos"))]
    {
        let _ = app; // unused on non-macOS
    }
    Ok(())
}

#[tauri::command]
async fn stop_backend(backend: tauri::State<'_, SharedBackend>) -> Result<(), String> {
    backend.lock().await.stop_all().await;
    Ok(())
}

#[tauri::command]
async fn check_health(api_url: String) -> Result<serde_json::Value, String> {
    let url = format!(
        "{}/health",
        if api_url.is_empty() {
            api_base()
        } else {
            api_url
        }
    );
    let resp = reqwest::get(&url)
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_energy(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/telemetry/energy", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_telemetry(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/telemetry/stats", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_traces(api_url: String, limit: u32) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/traces?limit={}", base, limit))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_trace(api_url: String, trace_id: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/traces/{}", base, trace_id))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_learning_stats(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/learning/stats", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_learning_policy(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/learning/policy", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_memory_stats(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/memory/stats", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn search_memory(
    api_url: String,
    query: String,
    top_k: u32,
) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let client = reqwest::Client::new();
    let resp = client
        .post(format!("{}/v1/memory/search", base))
        .json(&serde_json::json!({"query": query, "top_k": top_k}))
        .send()
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_agents(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/agents", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn fetch_models(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/models", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

#[tauri::command]
async fn run_jarvis_command(args: Vec<String>) -> Result<String, String> {
    let mut cmd_args = vec!["run".to_string(), "jarvis".to_string()];
    cmd_args.extend(args);
    let uv_bin = resolve_bin("uv");
    let output = tokio::process::Command::new(&uv_bin)
        .args(&cmd_args)
        .output()
        .await
        .map_err(|e| format!("Failed to launch jarvis: {}", e))?;

    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).to_string())
    } else {
        Err(String::from_utf8_lossy(&output.stderr).to_string())
    }
}

#[tauri::command]
async fn fetch_savings(api_url: String) -> Result<serde_json::Value, String> {
    let base = if api_url.is_empty() {
        api_base()
    } else {
        api_url
    };
    let resp = reqwest::get(format!("{}/v1/savings", base))
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    resp.json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))
}

/// Transcribe audio via the speech API endpoint.
#[tauri::command]
async fn transcribe_audio(
    api_url: String,
    audio_data: Vec<u8>,
    filename: String,
    language: Option<String>,
) -> Result<serde_json::Value, String> {
    let url = format!("{}/v1/speech/transcribe", api_url);
    let client = reqwest::Client::new();

    let part = reqwest::multipart::Part::bytes(audio_data)
        .file_name(filename)
        .mime_str("audio/webm")
        .map_err(|e| format!("Failed to create multipart: {}", e))?;

    let mut form = reqwest::multipart::Form::new().part("file", part);
    if let Some(lang) = language.filter(|l| !l.is_empty()) {
        form = form.text("language", lang);
    }

    let resp = client
        .post(&url)
        .multipart(form)
        .send()
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))?;
    Ok(body)
}

/// Submit savings to Supabase leaderboard.
#[tauri::command]
async fn submit_savings(
    supabase_url: String,
    supabase_key: String,
    payload: serde_json::Value,
) -> Result<bool, String> {
    if supabase_url.is_empty() || supabase_key.is_empty() {
        return Ok(false);
    }
    let client = reqwest::Client::new();
    let resp = client
        .post(format!(
            "{}/rest/v1/savings_entries?on_conflict=anon_id",
            supabase_url
        ))
        .header("Content-Type", "application/json")
        .header("apikey", &supabase_key)
        .header("Authorization", format!("Bearer {}", supabase_key))
        .header("Prefer", "resolution=merge-duplicates")
        .json(&payload)
        .send()
        .await
        .map_err(|e| format!("Supabase POST failed: {}", e))?;
    Ok(resp.status().is_success())
}

// ---------------------------------------------------------------------------
// Cloud API key management
// ---------------------------------------------------------------------------

/// Path to the cloud keys file (~/.openjarvis/cloud-keys.env).
fn cloud_keys_path() -> std::path::PathBuf {
    let home = home_dir();
    std::path::PathBuf::from(home)
        .join(".openjarvis")
        .join("cloud-keys.env")
}

/// Read cloud keys from disk and return as key=value pairs.
fn read_cloud_keys() -> Vec<(String, String)> {
    let path = cloud_keys_path();
    let mut keys = Vec::new();
    if let Ok(contents) = std::fs::read_to_string(&path) {
        for line in contents.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            if let Some((k, v)) = line.split_once('=') {
                keys.push((k.trim().to_string(), v.trim().to_string()));
            }
        }
    }
    keys
}

/// Save a single cloud API key to the keys file.
#[tauri::command]
async fn save_cloud_key(key_name: String, key_value: String) -> Result<(), String> {
    let path = cloud_keys_path();
    // Ensure directory exists
    if let Some(parent) = path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }

    // Read existing keys, update/add the one being saved
    let mut keys: Vec<(String, String)> = read_cloud_keys()
        .into_iter()
        .filter(|(k, _)| k != &key_name)
        .collect();
    if !key_value.is_empty() {
        keys.push((key_name, key_value));
    }

    // Write back
    let content: String = keys
        .iter()
        .map(|(k, v)| format!("{}={}", k, v))
        .collect::<Vec<_>>()
        .join("\n");
    std::fs::write(&path, content + "\n").map_err(|e| format!("Failed to save key: {}", e))?;

    // Set permissions to owner-only (chmod 600)
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600));
    }

    // Tell the running server to hot-reload its cloud engine so the user
    // doesn't need to restart the app after entering an API key.
    let reload_url = format!("http://127.0.0.1:{}/v1/cloud/reload", JARVIS_PORT);
    let _ = reqwest::Client::new()
        .post(&reload_url)
        .timeout(std::time::Duration::from_secs(10))
        .send()
        .await;

    Ok(())
}

/// Get which cloud providers have keys configured (without exposing values).
#[tauri::command]
async fn get_cloud_key_status() -> Result<serde_json::Value, String> {
    let keys = read_cloud_keys();
    let status: Vec<serde_json::Value> = keys
        .iter()
        .map(|(k, v)| serde_json::json!({ "key": k, "set": !v.is_empty() }))
        .collect();
    Ok(serde_json::json!(status))
}

/// Pull a model via Ollama (called from frontend download button).
#[tauri::command]
async fn pull_ollama_model(model_name: String) -> Result<serde_json::Value, String> {
    pull_model(&model_name)
        .await
        .map_err(|e| format!("Failed to pull {}: {}", model_name, e))?;
    Ok(serde_json::json!({"status": "ok", "model": model_name}))
}

/// Delete a model from Ollama.
#[tauri::command]
async fn delete_ollama_model(model_name: String) -> Result<serde_json::Value, String> {
    let url = format!("http://127.0.0.1:{}/api/delete", OLLAMA_PORT);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .delete(&url)
        .json(&serde_json::json!({"name": model_name}))
        .send()
        .await
        .map_err(|e| format!("Delete failed: {}", e))?;
    if !resp.status().is_success() {
        return Err(format!("Delete returned status {}", resp.status()));
    }
    Ok(serde_json::json!({"status": "deleted", "model": model_name}))
}

/// Check speech backend health.
#[tauri::command]
async fn speech_health(api_url: String) -> Result<serde_json::Value, String> {
    let url = format!("{}/v1/speech/health", api_url);
    let resp = reqwest::get(&url)
        .await
        .map_err(|e| format!("Connection failed: {}", e))?;
    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("Invalid response: {}", e))?;
    Ok(body)
}

// ---------------------------------------------------------------------------
// Notch presence — a click-through transparent window hugging the top-centre
// of the primary display (the MacBook notch).  It renders /notch, which draws
// a Dynamic-Island-style pill that grows out of the notch while JARVIS
// listens, thinks or speaks.  The main window drives it with `notch:state`
// events; the pill sends `notch:command` back (stop, listen, send text, a
// dropped file).  Only the drawn pill takes clicks; it takes keyboard focus
// only while its text box is in use.
// ---------------------------------------------------------------------------

#[cfg(target_os = "macos")]
mod notch {
    use objc::{msg_send, sel, sel_impl};
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::time::{Duration, Instant};
    use tauri::{Emitter, LogicalPosition, WebviewUrl, WebviewWindow, WebviewWindowBuilder};

    pub const LABEL: &str = "notch";
    /// Cursor entered (true) or left (false) the pill.  Sent from here because
    /// the unfocused webview gets no reliable mouseenter/mouseleave.
    const HOVER_EVENT: &str = "notch:hover";
    /// Cursor direction from the notch, `[x, y]` in -1..1, while it is
    /// within GAZE_RANGE points; the reactor leans that way.
    const GAZE_EVENT: &str = "notch:gaze";
    const GAZE_RANGE: f64 = 420.0;
    /// Room for the expanded pill; the page draws inside it.
    const WIDTH: f64 = 560.0;
    const HEIGHT: f64 = 480.0;
    /// Above the menu bar (NSMainMenuWindowLevel = 24, status items = 25).
    const LEVEL: i64 = 27;
    /// canJoinAllSpaces | stationary | ignoresCycle | fullScreenAuxiliary
    const BEHAVIOR: u64 = 1 | 16 | 64 | 256;
    /// How often to follow display changes (monitor plugged in / removed).
    const FOLLOW_EVERY: Duration = Duration::from_secs(3);

    #[repr(C)]
    #[derive(Copy, Clone)]
    struct CGPoint {
        x: f64,
        y: f64,
    }
    #[repr(C)]
    #[derive(Copy, Clone)]
    struct CGSize {
        width: f64,
        height: f64,
    }
    #[repr(C)]
    #[derive(Copy, Clone)]
    struct CGRect {
        origin: CGPoint,
        size: CGSize,
    }

    #[link(name = "CoreGraphics", kind = "framework")]
    extern "C" {
        fn CGGetActiveDisplayList(max: u32, displays: *mut u32, count: *mut u32) -> i32;
        fn CGDisplayIsBuiltin(display: u32) -> i32;
        fn CGMainDisplayID() -> u32;
        fn CGDisplayBounds(display: u32) -> CGRect;
        fn CGEventCreate(source: *const std::ffi::c_void) -> *mut std::ffi::c_void;
        fn CGEventGetLocation(event: *mut std::ffi::c_void) -> CGPoint;
    }
    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFRelease(cf: *const std::ffi::c_void);
    }

    /// Size of the pill as drawn (points), reported by the page; the window
    /// takes clicks only while the cursor is over it.  Stored as f32 bits.
    static HIT_W: AtomicU32 = AtomicU32::new(0);
    static HIT_H: AtomicU32 = AtomicU32::new(0);
    const TRACK_EVERY: Duration = Duration::from_millis(60);

    pub fn set_hit_area(width: f64, height: f64) {
        HIT_W.store((width as f32).to_bits(), Ordering::Relaxed);
        HIT_H.store((height as f32).to_bits(), Ordering::Relaxed);
    }

    /// Global cursor position, top-left origin (same space as CGDisplayBounds).
    fn cursor() -> Option<CGPoint> {
        unsafe {
            let ev = CGEventCreate(std::ptr::null());
            if ev.is_null() {
                return None;
            }
            let p = CGEventGetLocation(ev);
            CFRelease(ev);
            Some(p)
        }
    }

    /// Top-left of the pill window: centred on the built-in display (the one
    /// with the notch) when it is active, else on the main display.  Global
    /// display coordinates are points from the main display's top-left, the
    /// same space as Tauri logical positions.
    fn target() -> (f64, f64) {
        unsafe {
            let mut ids = [0u32; 16];
            let mut count = 0u32;
            let mut display = CGMainDisplayID();
            if CGGetActiveDisplayList(ids.len() as u32, ids.as_mut_ptr(), &mut count) == 0 {
                if let Some(id) = ids[..count as usize]
                    .iter()
                    .copied()
                    .find(|&id| CGDisplayIsBuiltin(id) != 0)
                {
                    display = id;
                }
            }
            let b = CGDisplayBounds(display);
            (b.origin.x + (b.size.width - WIDTH) / 2.0, b.origin.y)
        }
    }

    /// Centre of the notch row in global logical points (top-left origin):
    /// where the launch intro's reactor docks.
    pub fn anchor() -> (f64, f64) {
        let (x, y) = target();
        (x + WIDTH / 2.0, y + 19.0)
    }

    fn place(win: &WebviewWindow, (x, y): (f64, f64)) {
        let _ = win.set_position(LogicalPosition::new(x, y));
    }

    pub fn create(app: &tauri::App) -> tauri::Result<()> {
        let at = target();
        let win = WebviewWindowBuilder::new(app, LABEL, WebviewUrl::App("notch".into()))
            .title("JARVIS notch")
            .inner_size(WIDTH, HEIGHT)
            .position(at.0, at.1)
            .decorations(false)
            .transparent(true)
            .shadow(false)
            .resizable(false)
            .always_on_top(true)
            .visible_on_all_workspaces(true)
            .skip_taskbar(true)
            .focused(false)
            // The pill's buttons and input work on the first click.
            .accept_first_mouse(true)
            .build()?;
        win.set_ignore_cursor_events(true)?;
        place(&win, at);

        if let Ok(ns) = win.ns_window() {
            let ns = ns as *mut objc::runtime::Object;
            unsafe {
                let _: () = msg_send![ns, setLevel: LEVEL];
                let _: () = msg_send![ns, setCollectionBehavior: BEHAVIOR];
                let _: () = msg_send![ns, setHasShadow: false];
            }
        }

        // Follow the notch display when monitors are plugged in or removed,
        // and let clicks through everywhere except over the drawn pill.
        std::thread::spawn(move || {
            let mut last = at;
            let mut followed = Instant::now();
            let mut clickable = false;
            let mut gaze = (0.0f64, 0.0f64);
            loop {
                std::thread::sleep(TRACK_EVERY);
                if followed.elapsed() >= FOLLOW_EVERY {
                    followed = Instant::now();
                    let now = target();
                    if (now.0 - last.0).abs() > 0.5 || (now.1 - last.1).abs() > 0.5 {
                        place(&win, now);
                        last = now;
                    }
                }
                let w = f32::from_bits(HIT_W.load(Ordering::Relaxed)) as f64;
                let h = f32::from_bits(HIT_H.load(Ordering::Relaxed)) as f64;
                let at = cursor();
                let over = at.is_some_and(|p| {
                    let left = last.0 + (WIDTH - w) / 2.0;
                    p.x >= left && p.x <= left + w && p.y >= last.1 && p.y <= last.1 + h
                });
                let next = at
                    .map(|p| (p.x - (last.0 + WIDTH / 2.0), p.y - (last.1 + 19.0)))
                    .filter(|(dx, dy)| dx.hypot(*dy) < GAZE_RANGE)
                    .map(|(dx, dy)| ((dx / GAZE_RANGE).clamp(-1.0, 1.0), (dy / GAZE_RANGE).clamp(-1.0, 1.0)))
                    .unwrap_or((0.0, 0.0));
                if (next.0 - gaze.0).abs() > 0.02 || (next.1 - gaze.1).abs() > 0.02 || (next != gaze && next == (0.0, 0.0)) {
                    gaze = next;
                    let _ = win.emit_to(LABEL, GAZE_EVENT, [gaze.0, gaze.1]);
                }
                if over != clickable {
                    clickable = over;
                    let _ = win.set_ignore_cursor_events(!over);
                    let _ = win.emit_to(LABEL, HOVER_EVENT, over);
                }
            }
        });
        Ok(())
    }
}

/// The notch page reports the pill's drawn size so only it takes clicks.
#[tauri::command]
fn notch_hit_area(width: f64, height: f64) {
    #[cfg(target_os = "macos")]
    notch::set_hit_area(width, height);
    #[cfg(not(target_os = "macos"))]
    let _ = (width, height);
}

/// The notch's text box takes the keyboard (true) or hands it back to the app
/// the user was in (false), so typing to JARVIS never strands the focus.
#[tauri::command]
fn notch_keyboard(app: tauri::AppHandle, focus: bool) {
    if focus {
        if let Some(win) = app.get_webview_window("notch") {
            let _ = win.set_focus();
        }
        return;
    }
    #[cfg(target_os = "macos")]
    let _ = app.run_on_main_thread(|| unsafe {
        use objc::{class, msg_send, sel, sel_impl};
        let ns_app: *mut objc::runtime::Object = msg_send![class!(NSApplication), sharedApplication];
        let _: () = msg_send![ns_app, deactivate];
    });
}

/// The user's own launch-intro sound, `~/.openjarvis/boot-sound.mp3`, as raw
/// bytes (an ArrayBuffer in JS).  Optional and never shipped: when it is
/// missing the command errors and the intro plays its synthesized score.
#[tauri::command]
fn boot_sound() -> Result<tauri::ipc::Response, String> {
    let home = std::env::var("HOME").map_err(|e| e.to_string())?;
    let path = std::path::Path::new(&home).join(".openjarvis/boot-sound.mp3");
    std::fs::read(path)
        .map(tauri::ipc::Response::new)
        .map_err(|e| e.to_string())
}

/// Where the notch is on screen (global logical points), so the launch
/// intro can fly its reactor towards it.  None off macOS.
#[tauri::command]
fn notch_anchor() -> Option<(f64, f64)> {
    #[cfg(target_os = "macos")]
    return Some(notch::anchor());
    #[cfg(not(target_os = "macos"))]
    None
}

// ---------------------------------------------------------------------------
// Main window — the "advanced" view.  JARVIS lives in the notch; the big
// window (history, settings, dashboard…) is opened from the notch, the menu
// bar item, Cmd+Shift+J or the Dock, and closing it only hides it.  It must
// stay alive while hidden: it runs the voice, wake word and chat the notch
// delegates to (see backgroundThrottling in tauri.conf.json).
// ---------------------------------------------------------------------------

/// Tells the main page what happened to its window: "hidden" (it returns to
/// the chat, which handles the notch's commands), "shown" or "settings".
const WINDOW_EVENT: &str = "jarvis-window";
/// Tells the notch whether the main window is open (its ⤢ / ⤡ button).
const NOTCH_MAIN_EVENT: &str = "notch:main-window";

fn show_main(app: &tauri::AppHandle, page: Option<&str>) {
    use tauri::Emitter;
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.unminimize();
        let _ = win.show();
        let _ = win.set_focus();
        let _ = app.emit_to("main", WINDOW_EVENT, page.unwrap_or("shown"));
        let _ = app.emit_to("notch", NOTCH_MAIN_EVENT, true);
    }
}

fn hide_main(app: &tauri::AppHandle) {
    use tauri::Emitter;
    if let Some(win) = app.get_webview_window("main") {
        let _ = app.emit_to("main", WINDOW_EVENT, "hidden");
        let _ = app.emit_to("notch", NOTCH_MAIN_EVENT, false);
        let _ = win.hide();
    }
}

/// Shows (true), hides (false) or toggles (null) the main window.  Toggling
/// a window that is open but behind other apps brings it forward.
#[tauri::command]
fn main_window(app: tauri::AppHandle, visible: Option<bool>) {
    let shown = app.get_webview_window("main").is_some_and(|w| {
        w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false)
    });
    if visible.unwrap_or(!shown) {
        show_main(&app, None);
    } else {
        hide_main(&app);
    }
}

// ---------------------------------------------------------------------------
// App entry point
// ---------------------------------------------------------------------------

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let backend: SharedBackend = Arc::new(Mutex::new(BackendManager::default()));
    let status: SharedStatus = Arc::new(Mutex::new(SetupStatus::default()));

    let boot_backend_ref = backend.clone();
    let boot_status_ref = status.clone();
    #[cfg(target_os = "macos")]
    let wake_backend_ref = backend.clone();

    tauri::Builder::default()
        .manage(backend.clone())
        .manage(status.clone())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec!["--hidden"]),
        ))
        // .plugin(tauri_plugin_updater::Builder::new().build()) // disabled for local dev
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_dialog::init())
        // Launching JARVIS again (Spotlight, Finder) opens the main window.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main(app, None);
        }))
        .on_window_event(|window, event| {
            // The red traffic light hides the window instead of closing it:
            // JARVIS keeps running in the notch.  Quitting for real goes
            // through the menu bar item's "Salir" or Cmd+Q.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                if window.label() == "main" {
                    hide_main(window.app_handle());
                } else {
                    let _ = window.hide();
                }
            }
        })
        .setup(move |app| {
            // System tray
            let show = MenuItemBuilder::with_id("show", "Abrir JARVIS").build(app)?;
            let settings =
                MenuItemBuilder::with_id("settings", "Configuración…").build(app)?;
            let health = MenuItemBuilder::with_id("health", "Health: starting...")
                .enabled(false)
                .build(app)?;
            let quit = MenuItemBuilder::with_id("quit", "Salir de JARVIS").build(app)?;

            let menu = MenuBuilder::new(app)
                .item(&show)
                .item(&settings)
                .separator()
                .item(&health)
                .separator()
                .item(&quit)
                .build()?;

            let _tray = TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("OpenJarvis")
                .menu(&menu)
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "show" => show_main(app, None),
                    "settings" => show_main(app, Some("settings")),
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            // Notch presence pill (driven by the main window).
            #[cfg(target_os = "macos")]
            if let Err(e) = notch::create(app) {
                eprintln!("[notch] could not create the notch window: {e}");
            }

            // Cmd+Shift+J opens / hides the main window from anywhere.
            {
                use tauri_plugin_global_shortcut::{
                    Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState,
                };
                let sc = Shortcut::new(Some(Modifiers::META | Modifiers::SHIFT), Code::KeyJ);
                if let Err(e) = app.global_shortcut().on_shortcut(sc, |app, _sc, ev| {
                    if ev.state == ShortcutState::Pressed {
                        main_window(app.clone(), None);
                    }
                }) {
                    eprintln!("Warning: could not register Cmd+Shift+J: {e}");
                }
            }

            // Auto-start backend services on launch
            tauri::async_runtime::spawn(boot_backend(boot_backend_ref, boot_status_ref));

            // Auto-start the macOS wake-word sidecar.  Previously this was
            // only spawned from the ``start_backend`` command, but the
            // frontend never invokes that command — it relies on the
            // ``setup`` hook's auto-boot path above.  Without this spawn the
            // JS ``useWakeWord`` hook listens for ``jarvis-wake`` events
            // forever and the wake word silently never fires.
            #[cfg(target_os = "macos")]
            {
                let app_handle = app.handle().clone();
                tauri::async_runtime::spawn(spawn_wake_listener(app_handle, wake_backend_ref));
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            notch_hit_area,
            notch_keyboard,
            main_window,
            notch_anchor,
            boot_sound,
            get_setup_status,
            choose_engine,
            get_api_base,
            wake_ready,
            start_backend,
            stop_backend,
            check_health,
            fetch_energy,
            fetch_telemetry,
            fetch_traces,
            fetch_trace,
            fetch_learning_stats,
            fetch_learning_policy,
            fetch_memory_stats,
            search_memory,
            fetch_agents,
            fetch_models,
            run_jarvis_command,
            fetch_savings,
            submit_savings,
            transcribe_audio,
            speech_health,
            pull_ollama_model,
            delete_ollama_model,
            save_cloud_key,
            get_cloud_key_status,
        ])
        .build(tauri::generate_context!())
        .expect("error while building OpenJarvis Desktop")
        .run(move |app, event| {
            // Clicking the Dock icon with no window open shows the main one.
            #[cfg(target_os = "macos")]
            if let tauri::RunEvent::Reopen { has_visible_windows, .. } = event {
                let main_visible = app
                    .get_webview_window("main")
                    .is_some_and(|w| w.is_visible().unwrap_or(false));
                if !main_visible {
                    show_main(app, None);
                }
                let _ = has_visible_windows; // the notch window always counts
                return;
            }
            if let tauri::RunEvent::ExitRequested { .. } = event {
                let b = backend.clone();
                tauri::async_runtime::spawn(async move {
                    b.lock().await.stop_all().await;
                });
            }
        });
}
