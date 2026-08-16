use clap::Parser;

mod cli;
use cli::{handle_cli, CliArgs};

#[tokio::main]
async fn main() {
    // Fix: WebKitGTK PipeWire DMA-BUF memory allocation bug.
    // Without this, getDisplayMedia() causes GStreamer CRITICAL errors.
    // Forces WebKit to use shared memory instead of DMA-BUF for PipeWire frames.
    std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");

    // Also suppress GStreamer noise in terminal output
    if std::env::var("GST_DEBUG").is_err() {
        std::env::set_var("GST_DEBUG", "0");
    }

    // Parse CLI arguments first.
    // If a subcommand is present, run in headless CLI mode and exit.
    let cli_args = CliArgs::parse();
    if handle_cli(cli_args) {
        return;
    }

    // No subcommand → launch full Tauri desktop GUI application
    nearby_cast_lib::run();
}
