#if canImport(UIKit)
import SwiftUI
import WebKit

/// One of the product's web areas, embedded.
///
/// The board, the mesh, the map and the atlas are one application on the web
/// and in the macOS Cockpit. This client used to offer only its own native
/// board, so the phone was the one surface still showing the old world. The
/// three graph areas are the web build embedded against the same endpoint the
/// native views already read, so there is one product rather than a phone app
/// beside a separate website.
struct IOSWebSurface: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.isOpaque = false
        view.backgroundColor = .black
        view.scrollView.backgroundColor = .black
        view.allowsBackForwardNavigationGestures = false
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        // Reload only when the destination actually changes; SwiftUI calls this
        // on every layout pass, and reloading a graph on each one would keep it
        // permanently in its first read.
        guard context.coordinator.loaded != url else { return }
        context.coordinator.loaded = url
        // Generous, because a real board's graph documents are not small and a
        // short timeout reports a healthy board as unavailable.
        view.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 45))
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    final class Coordinator {
        var loaded: URL?
    }
}

/// A web area wrapped in the client's own chrome, so an area that cannot load
/// says so in this app's voice rather than showing a blank browser.
struct IOSAreaScreen: View {
    let title: String
    let path: String
    @ObservedObject var model: CockpitModel

    private var destination: URL? {
        guard let base = model.baseURL else { return nil }
        return URL(string: path, relativeTo: base)
    }

    var body: some View {
        ZStack {
            CoordCanvas()
            if let destination {
                IOSWebSurface(url: destination)
                    .ignoresSafeArea(edges: .bottom)
            } else {
                VStack(spacing: Space.sm) {
                    Text("No endpoint set")
                        .font(.system(size: 15, weight: .medium))
                        .foregroundStyle(Theme.textHi)
                    Text("Set a base URL in Settings, then reopen \(title).")
                        .font(.footnote)
                        .foregroundStyle(Theme.muted)
                        .multilineTextAlignment(.center)
                }
                .padding(Space.xl)
            }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.hidden, for: .navigationBar)
    }
}
#endif
