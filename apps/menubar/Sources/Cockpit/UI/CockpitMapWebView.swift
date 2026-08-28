import AppKit
import WebKit

final class CockpitMapWebView: NSView, WKNavigationDelegate {
    private var webView: WKWebView?
    /// Which web surface this view mounts. The cockpit embeds several (map,
    /// mesh, atlas); they differ only by path, so one view serves them all and
    /// reloads when the path changes.
    var surfacePath: String = "/cockpit?native_map=1" {
        didSet {
            guard surfacePath != oldValue else { return }
            hasLoaded = false
            if isActive { loadIfNeeded(force: true) }
        }
    }
    private let fallback = CockpitUI.label("Loading Product Map...", size: 13, weight: .semibold, color: CockpitTokens.Color.muted, align: .center)
    /// The name of the surface being mounted, used verbatim in status and
    /// failure text. A single hardcoded name meant every surface reported
    /// itself as the Product Map when it failed.
    var surfaceLabel: String = "Product Map"
    /// The URL the last load actually attempted, so a failure can name it
    /// rather than guessing at a port.
    private var lastTarget: URL?
    private var hasLoaded = false
    private var lastOpenScriptAt = Date.distantPast
    private var isActive = false
    private var unloadTimer: Timer?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = CockpitTokens.Color.bg.cgColor
        addSubview(fallback)
        fallback.isHidden = true
    }

    required init?(coder: NSCoder) { nil }
    override var isFlipped: Bool { true }

    override func layout() {
        super.layout()
        webView?.frame = bounds
        fallback.frame = bounds.insetBy(dx: 24, dy: 24)
    }

    func render(_ state: CockpitMapState) {
        activate(forceReload: state.error != nil)
    }

    func reloadProductMap() {
        activate(forceReload: true)
    }

    func activate() {
        activate(forceReload: false)
    }

    private func activate(forceReload: Bool) {
        isActive = true
        unloadTimer?.invalidate()
        unloadTimer = nil
        loadIfNeeded(force: forceReload)
    }

    func deactivate(unloadAfter delay: TimeInterval = CockpitMapLifecycle.defaultIdleUnloadDelay) {
        unloadNow()
    }

    func unloadNow() {
        unloadTimer?.invalidate()
        unloadTimer = nil
        isActive = false
        suspendGraphWork()
        webView?.stopLoading()
        webView?.navigationDelegate = nil
        webView?.removeFromSuperview()
        webView = nil
        hasLoaded = false
        fallback.isHidden = true
    }

    private func loadIfNeeded(force: Bool = false) {
        guard isActive else { return }
        let webView = ensureWebView()
        guard force || !hasLoaded else {
            prepareLoadedSurface()
            return
        }
        hasLoaded = true
        fallback.isHidden = false
        guard let url = URL(string: "\(HarnessEndpoint.base)\(surfacePath)") else { return }
        lastTarget = url
        fallback.stringValue = "Loading \(surfaceLabel)..."
        webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 45))
    }

    private func ensureWebView() -> WKWebView {
        if let webView { return webView }
        let configuration = WKWebViewConfiguration()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.websiteDataStore = .default()
        let view = WKWebView(frame: bounds, configuration: configuration)
        view.navigationDelegate = self
        view.allowsBackForwardNavigationGestures = false
        view.setValue(false, forKey: "drawsBackground")
        addSubview(view, positioned: .below, relativeTo: fallback)
        webView = view
        return view
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        fallback.isHidden = true
        prepareLoadedSurface()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showLoadFailure(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showLoadFailure(error)
    }

    private func showLoadFailure(_ error: Error) {
        fallback.isHidden = false
        fallback.stringValue = failureText(error.localizedDescription)
    }

    /// Say which surface failed and which address it tried. The old text named
    /// one surface and one port for every failure, so a mesh that could not
    /// reach its own origin told the operator to start a different server on a
    /// port that was already serving.
    private func failureText(_ detail: String) -> String {
        guard let target = lastTarget else {
            return "\(surfaceLabel) could not load.\n\(detail)"
        }
        let host = target.host ?? "127.0.0.1"
        let port = target.port.map(String.init) ?? "80"
        return """
        \(surfaceLabel) could not load from \(target.absoluteString)
        \(detail)
        Nothing is answering on \(host):\(port). Start that server, then reopen \(surfaceLabel).
        """
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
        // A 404 finishes navigation successfully, so an unrouted path used to
        // render the server's error body as though it were the surface. Treat
        // any non-2xx as the failure it is.
        guard navigationResponse.isForMainFrame,
              let http = navigationResponse.response as? HTTPURLResponse,
              !(200...299).contains(http.statusCode) else {
            decisionHandler(.allow)
            return
        }
        decisionHandler(.cancel)
        fallback.isHidden = false
        fallback.stringValue = failureText(
            "The server answered HTTP \(http.statusCode) -- this address is not a surface it serves."
        )
    }

    private func suspendGraphWork() {
        guard let webView else { return }
        let script = """
        (() => {
          try {
            if (typeof window.coordharnessNativeMapSuspend === 'function') {
              window.coordharnessNativeMapSuspend();
              return true;
            }
            if (typeof closeMapDrawer === 'function') closeMapDrawer();
            if (typeof closeCmdk === 'function') closeCmdk();
            if (typeof _cy !== 'undefined' && _cy) { try { _cy.destroy(); } catch (_) {} _cy = null; }
            if (typeof _cyk !== 'undefined' && _cyk) { try { _cyk.destroy(); } catch (_) {} _cyk = null; }
            return true;
          } catch (_) {
            return false;
          }
        })();
        """
        webView.evaluateJavaScript(script, completionHandler: nil)
    }

    private var isProductMapSurface: Bool {
        guard let components = URLComponents(string: surfacePath) else { return false }
        return components.path == "/cockpit"
            && components.queryItems?.contains(where: {
                $0.name == "native_map" && $0.value == "1"
            }) == true
    }

    private func prepareLoadedSurface() {
        guard isProductMapSurface else { return }
        openProductMap()
    }

    private func openProductMap(attempt: Int = 0) {
        guard isActive, let webView else { return }
        guard attempt > 0 || Date().timeIntervalSince(lastOpenScriptAt) > 0.4 else { return }
        lastOpenScriptAt = Date()
        let script = """
        (async () => {
          const closeOverlay = (id) => {
            const el = document.getElementById(id);
            if (!el) return;
            el.setAttribute('aria-hidden', 'true');
            el.classList.remove('open', 'show', 'on');
          };
          try {
            if (typeof closeCmdk === 'function') closeCmdk();
            if (typeof closeAlerts === 'function') closeAlerts();
            if (typeof closeAudit === 'function') closeAudit();
          } catch (_) {}
          closeOverlay('cmdkOverlay');
          closeOverlay('alertsOverlay');
          closeOverlay('auditOverlay');
          document.body.classList.add('native-map-shell');
          if (!document.getElementById('native-map-shell-style')) {
            const style = document.createElement('style');
            style.id = 'native-map-shell-style';
            style.textContent = `
              body.native-map-shell .top { display: none !important; }
              body.native-map-shell .wrap { width: min(1740px, calc(100vw - 34px)); padding-top: 12px; }
              body.native-map-shell .map-bar { margin-top: 0; flex-wrap: wrap; align-items: center; gap: 10px; }
              body.native-map-shell .map-bar h1 { flex: 0 0 auto; margin-right: 6px; }
              body.native-map-shell #mapSeg { flex: 1 1 760px; min-width: min(100%, 620px); overflow: visible; flex-wrap: wrap; row-gap: 6px; }
              body.native-map-shell #mapSeg button { padding-left: 11px; padding-right: 11px; }
              body.native-map-shell #mapSeg::-webkit-scrollbar { display: none; }
              body.native-map-shell .map-spacer { display: none !important; }
              body.native-map-shell #mapBackBtn { display: none !important; }
              body.native-map-shell #mapCmdk,
              body.native-map-shell #mapFocus,
              body.native-map-shell #mapExport,
              body.native-map-shell #mapAudit,
              body.native-map-shell #mapRefresh { padding-left: 11px; padding-right: 11px; }
            `;
            document.head.appendChild(style);
          }
          try {
            if (typeof openMapView === 'function') {
              await openMapView();
            } else {
              const button = document.getElementById('mapviewbtn');
              if (button) button.click();
            }
            if (typeof closeMapDrawer === 'function') closeMapDrawer();
          } catch (error) {
            return { ok: false, error: String(error), hasOpenMapView: typeof openMapView };
          }
          document.body.classList.add('map-active');
          return {
            ok: document.body.classList.contains('map-active') && !!document.getElementById('mapView'),
            hasOpenMapView: typeof openMapView,
            classes: document.body.className
          };
        })();
        """
        webView.evaluateJavaScript(script) { [weak self] result, error in
            let ok = (result as? [String: Any])?["ok"] as? Bool
            if error != nil || ok != true {
                guard attempt < 20 else { return }
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
                    self?.openProductMap(attempt: attempt + 1)
                }
            }
        }
    }
}
