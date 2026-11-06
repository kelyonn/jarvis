// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "JarvisShell",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "JarvisShell", targets: ["JarvisShell"]),
    ],
    targets: [
        // Pure-Swift, platform-independent logic (wire protocol, backoff, snap geometry, safety lists).
        .target(name: "JarvisShellCore"),
        // The menu-bar app: audio I/O, orb, hotkey, lifecycle, AX, supervisor.
        .executableTarget(name: "JarvisShell", dependencies: ["JarvisShellCore"]),
        .testTarget(name: "JarvisShellCoreTests", dependencies: ["JarvisShellCore"]),
    ]
)
