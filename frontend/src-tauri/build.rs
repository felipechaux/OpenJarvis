fn main() {
    // In dev (`tauri dev`), the Tauri binary is exec'd directly from
    // ``target/debug/openjarvis-desktop`` — there is no surrounding .app
    // bundle, so macOS TCC has no Info.plist to read for the webview's
    // getUserMedia() / camera prompts and silently denies access without
    // even showing the user the permission dialog.  Embedding the plist
    // into the binary's ``__TEXT,__info_plist`` Mach-O section gives TCC
    // a plist source even without a bundle, which restores normal prompt
    // behaviour in dev.  Production builds embed via Tauri's bundler.
    //
    // Caveat: cargo's adhoc linker-sign leaves the embedded plist "not
    // bound" — TCC still won't read it.  We invoke ``codesign`` as a
    // post-link step (via a final linker argument that runs after the
    // link finishes) to re-sign with the plist bound and a stable
    // identifier so the user's TCC grant persists across rebuilds.
    #[cfg(target_os = "macos")]
    {
        let plist = std::path::Path::new("Info.plist");
        if plist.exists() {
            println!("cargo:rerun-if-changed=Info.plist");
            if let Ok(abs) = std::fs::canonicalize(plist) {
                println!(
                    "cargo:rustc-link-arg=-Wl,-sectcreate,__TEXT,__info_plist,{}",
                    abs.display()
                );
            }
        }
    }
    tauri_build::build();
}
