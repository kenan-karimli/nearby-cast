use clap::{Parser, Subcommand};
use std::net::TcpStream;
use std::time::Duration;
use std::{fs, process::Command};

#[derive(Parser, Debug)]
#[command(name = "nearby-cast")]
#[command(author = "Nearby Cast Team")]
#[command(version = "0.1.0")]
#[command(about = "Cast anything. Nearby. Real multi-protocol Linux screen casting.", long_about = None)]
pub struct CliArgs {
    #[command(subcommand)]
    pub command: Option<Commands>,
}

#[derive(Subcommand, Debug)]
pub enum Commands {
    /// List nearby compatible devices discovered on local network
    Devices,
    /// Display active screen casting status & statistics
    Status,
    /// Initiate screen cast to a specified device
    Cast {
        #[arg(short, long)]
        device: String,
    },
    /// Stop active casting session
    Stop,
    /// Initiate 6-digit pairing code verification with a device
    Pair { device: String },
    /// Revoke trust for a saved device
    Forget { device: String },
    /// Run full diagnostic checks against a specific device (or scan all)
    Diagnose {
        /// Optional target IP or device name. If omitted, scans all local network displays.
        device: Option<String>,
    },
}

fn tcp_reachable(ip: &str, port: u16) -> bool {
    TcpStream::connect_timeout(
        &format!("{}:{}", ip, port)
            .parse()
            .unwrap_or_else(|_| "0.0.0.0:80".parse().unwrap()),
        Duration::from_secs(2),
    )
    .is_ok()
}

fn run_diagnose(target_ip: &str) {
    println!("\nNearby Cast Diagnostics");
    println!("{}", "─".repeat(40));
    println!("Device IP: {}", target_ip);
    println!();

    // 1. Basic TCP reachability
    let reachable_80 = tcp_reachable(target_ip, 80);
    let reachable_8008 = tcp_reachable(target_ip, 8008);
    let reachable_8009 = tcp_reachable(target_ip, 8009);
    let reachable_7000 = tcp_reachable(target_ip, 7000); // AirPlay

    println!("Reachability:");
    println!(
        "  {} TCP :80   (HTTP)",
        if reachable_80 { "✓" } else { "✗" }
    );
    println!(
        "  {} TCP :8008 (Google Cast DIAL)",
        if reachable_8008 { "✓" } else { "✗" }
    );
    println!(
        "  {} TCP :8009 (Google Cast Channel)",
        if reachable_8009 { "✓" } else { "✗" }
    );
    println!(
        "  {} TCP :7000 (AirPlay)",
        if reachable_7000 { "✓" } else { "✗" }
    );
    println!();

    // 2. Google Cast DIAL description XML
    let dial_url = format!("http://{}:8008/ssdp/device-desc.xml", target_ip);
    let dial_result = Command::new("curl")
        .args(["-s", "--max-time", "3", &dial_url])
        .output()
        .ok();

    let dial_ok = dial_result
        .as_ref()
        .map(|o| o.status.success())
        .unwrap_or(false);
    let dial_body = dial_result
        .map(|o| String::from_utf8_lossy(&o.stdout).to_string())
        .unwrap_or_default();

    println!("DIAL Description XML (Google Cast):");
    if dial_ok && !dial_body.is_empty() {
        println!("  ✓ Fetched ({} bytes)", dial_body.len());
        // Extract friendly name from XML
        if let Some(start) = dial_body.find("<friendlyName>") {
            if let Some(end) = dial_body[start..].find("</friendlyName>") {
                let name = &dial_body[start + 14..start + end];
                println!("  Friendly Name: {}", name);
            }
        }
        if let Some(start) = dial_body.find("<modelName>") {
            if let Some(end) = dial_body[start..].find("</modelName>") {
                let model = &dial_body[start + 11..start + end];
                println!("  Model: {}", model);
            }
        }
    } else {
        println!("  ✗ Not reachable or invalid XML");
    }
    println!();

    // 3. pychromecast discovery
    let py_out = Command::new("python3")
        .arg("-c")
        .arg(format!(
            r#"
import pychromecast, sys
cs, browser = pychromecast.get_chromecasts(known_hosts=['{}'])
if cs:
    c = cs[0]
    c.wait(timeout=5)
    print(f'name={{c.name}}')
    print(f'model={{c.model_name}}')
    print(f'status={{c.status}}')
    print(f'is_idle={{c.is_idle}}')
pychromecast.discovery.stop_discovery(browser)
sys.exit(0 if cs else 1)
"#,
            target_ip
        ))
        .output()
        .ok();

    println!("Google Cast / Chromecast:");
    if let Some(ref out) = py_out {
        if out.status.success() {
            let stdout = String::from_utf8_lossy(&out.stdout);
            for line in stdout.lines() {
                println!("  {}", line);
            }
            println!("  ✓ Chromecast device confirmed");
        } else {
            println!("  ✗ No Chromecast response (pychromecast)");
            if !out.stderr.is_empty() {
                let err = String::from_utf8_lossy(&out.stderr);
                println!("  stderr: {}", err.lines().next().unwrap_or(""));
            }
        }
    } else {
        println!("  ✗ python3 / pychromecast not available");
    }
    println!();

    // 4. Protocol Summary
    let detected_protocol = if reachable_8008 || dial_ok {
        "Google Cast / Chromecast"
    } else if reachable_7000 {
        "AirPlay"
    } else {
        "Unknown"
    };
    println!("Detected Protocol: {}", detected_protocol);

    // 5. Screen mirroring verdict
    println!();
    println!("Screen Mirroring Support:");
    match detected_protocol {
        "Google Cast / Chromecast" => {
            println!("  ✓ Screen casting via HLS stream + pychromecast play_media");
            println!("  The Nearby Cast HLS server will stream your screen to the TV.");
        }
        "AirPlay" => {
            println!("  ✓ AirPlay lab mirroring (PIN + RTSP) when the receiver advertises lab=1");
            println!("  ✗ Physical FairPlay Apple TV authentication: NOT VERIFIED");
        }
        _ => {
            println!("  ✗ Protocol not identified — screen casting not supported.");
        }
    }
    println!();
}

pub fn handle_cli(cli: CliArgs) -> bool {
    if let Some(cmd) = cli.command {
        match cmd {
            Commands::Devices => {
                println!("Nearby Cast — Discovered Devices:");
                println!("  (Use the GUI for live mDNS device discovery)");
            }
            Commands::Status => {
                println!("Nearby Cast — Status:");
                match fs::read_to_string("/tmp/nearby_cast_status.json") {
                    Ok(status) => println!("  Managed session status: {status}"),
                    Err(_) => {
                        println!("  No NearbyCast-managed projection session is reporting status.")
                    }
                }
            }
            Commands::Cast { device } => {
                println!("Headless casting to {device} is not implemented. Use the GUI so NearbyCast can verify the receiver and source.");
            }
            Commands::Stop => {
                println!("Headless stop is unavailable because this command does not own an active GUI session. Use Stop Projection in NearbyCast.");
            }
            Commands::Pair { device } => {
                println!("Pairing with {device} is not implemented for the headless CLI.");
            }
            Commands::Forget { device } => {
                println!("Trust management for {device} is not implemented for the headless CLI.");
            }
            Commands::Diagnose { device } => {
                if let Some(target) = device {
                    run_diagnose(&target);
                } else {
                    eprintln!("A receiver IP address is required: nearby-cast diagnose <IP>");
                }
            }
        }
        true
    } else {
        false
    }
}
