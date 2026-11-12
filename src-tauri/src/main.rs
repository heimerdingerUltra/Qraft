#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    env, fs,
    io::{self, BufRead, BufReader},
    os::windows::process::CommandExt,
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::Mutex,
};
use tauri::{menu::MenuBuilder, Manager, WebviewUrl, WebviewWindowBuilder};

const CREATE_NO_WINDOW: u32 = 0x08000000;

struct Backend(Mutex<Child>);
impl Drop for Backend {
    fn drop(&mut self) {
        if let Ok(child) = self.0.get_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

fn project_directory() -> io::Result<PathBuf> {
    for dir in env::current_exe()?.ancestors().skip(1) {
        if dir.join("server.mjs").is_file() && dir.join("public/app.js").is_file() {
            return Ok(dir.to_owned());
        }
    }
    Err(io::Error::new(
        io::ErrorKind::NotFound,
        "Placez cet exécutable dans le dossier du projet.",
    ))
}

fn start_backend() -> io::Result<(Backend, String)> {
    let root = project_directory()?;
    let mut child = Command::new("node.exe")
        .current_dir(root)
        .args(["--env-file-if-exists=.env", "server.mjs"])
        .env("PORT", "0")
        .env("MEETING_DESKTOP_CHILD", "1")
        .creation_flags(CREATE_NO_WINDOW)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()?;
    let output = child.stdout.take().expect("stdout redirigé");
    let backend = Backend(Mutex::new(child));
    for line in BufReader::new(output).lines() {
        let line = line?;
        if let Some(port) = line.strip_prefix("http://127.0.0.1:") {
            if port.parse::<u16>().is_ok_and(|value| value > 0) {
                return Ok((backend, line));
            }
        }
    }
    Err(io::Error::other(
        "Le serveur Node.js ne démarre pas. Vérifiez npm ci et Node.js 24.",
    ))
}

fn open_chrome(url: &str) -> io::Result<()> {
    for variable in ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"] {
        if let Some(base) = env::var_os(variable) {
            let path = PathBuf::from(base).join("Google/Chrome/Application/chrome.exe");
            if fs::metadata(&path).is_ok() {
                Command::new(path).arg(url).spawn()?;
                return Ok(());
            }
        }
    }
    Err(io::Error::new(
        io::ErrorKind::NotFound,
        "Google Chrome introuvable",
    ))
}

fn show_error(message: &str) {
    #[link(name = "user32")]
    extern "system" {
        fn MessageBoxW(window: isize, text: *const u16, title: *const u16, flags: u32) -> i32;
    }
    let message: Vec<u16> = message.encode_utf16().chain(Some(0)).collect();
    let title: Vec<u16> = "Assistant de réunion"
        .encode_utf16()
        .chain(Some(0))
        .collect();
    // Both null-terminated strings remain alive for the synchronous Windows call.
    unsafe {
        MessageBoxW(0, message.as_ptr(), title.as_ptr(), 0x10);
    }
}

fn main() {
    let result = tauri::Builder::default()
        .setup(|app| {
            let (backend, url) = start_backend()?;
            app.manage(backend);
            let menu = MenuBuilder::new(app)
                .text("chrome", "Ouvrir dans Chrome pour Teams Web ou Meet")
                .build()?;
            app.set_menu(menu)?;
            let address = url.clone();
            app.on_menu_event(move |_, event| {
                if event.id().as_ref() == "chrome" {
                    if let Err(err) = open_chrome(&address) {
                        show_error(&err.to_string());
                    }
                }
            });
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(url.parse()?))
                .initialization_script("window.meetingDesktop = true;")
                .title("Assistant de réunion")
                .inner_size(920.0, 820.0)
                .min_inner_size(520.0, 480.0)
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!());
    if let Err(error) = result {
        show_error(&error.to_string());
    }
}
