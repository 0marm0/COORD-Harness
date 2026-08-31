import Foundation
import AppKit
import XCTest
@testable import CoordCockpitMac

final class UsageDashboardTests: XCTestCase {
    func testAccountSettingsHeaderStaysOutsideScrollableMetrics() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/Shared/Sources/UsageDashboardContent.swift"),
            encoding: .utf8
        )
        let bodyStart = try XCTUnwrap(source.range(of: "var body: some View"))
        let sectionEnd = try XCTUnwrap(source.range(of: "private func sectionSpacing"))
        let body = String(source[bodyStart.lowerBound..<sectionEnd.lowerBound])
        let pinnedHeader = try XCTUnwrap(body.range(of: "UsageDashboardHeader("))
        let scrollingMetrics = try XCTUnwrap(body.range(of: "ScrollView"))
        XCTAssertLessThan(pinnedHeader.lowerBound, scrollingMetrics.lowerBound)
        XCTAssertTrue(body.contains("Divider().opacity(0.28)"))
    }

    func testCanonicalProviderAwarePayloadDecodesWithoutCombiningProviders() throws {
        let snapshot = try fixture()
        let codex = try XCTUnwrap(snapshot.providers["codex"])
        let claude = try XCTUnwrap(snapshot.providers["claude"])

        XCTAssertEqual(snapshot.schema, UsageIntelligenceContract.identifier)
        XCTAssertEqual(UsageProviderCardState.cards(from: snapshot).map(\.id), ["claude", "codex"])
        XCTAssertEqual(codex.history?.todayTotalTokens, 15)
        XCTAssertEqual(codex.history?.daily.last?.totalTokens, 999, "Today must not be inferred from the last daily row")
        XCTAssertEqual(codex.history?.providerReportedAccount?.todayTotalTokens, 700)
        XCTAssertEqual(codex.history?.providerReportedAccount?.semantics, "provider_reported_account")
        XCTAssertEqual(claude.history?.semantics, "ever_observed_envelope")
        XCTAssertNil(claude.history?.allTimeTotalTokens)
        XCTAssertEqual(codex.quotaGroups.map(\.safeLabel), ["Account quota", "GPT-5.3-Codex-Spark"])
        XCTAssertEqual(codex.windows.first?.kind, "weekly", "Legacy windows must mirror only the first meter")
        XCTAssertEqual(codex.windows.first?.usedPercent, 19)
        XCTAssertEqual(codex.quotaGroups.first?.windows.map(\.kind), ["weekly"])
        XCTAssertEqual(codex.quotaGroups.first?.windows.map(\.usedPercent), [19])
        XCTAssertEqual(codex.quotaGroups.first?.windows.map(\.resolvedRemainingPercent), [81])
        XCTAssertEqual(codex.quotaGroups.last?.windows.map(\.kind), ["session", "weekly"])
        XCTAssertEqual(codex.quotaGroups.last?.windows.map(\.usedPercent), [0, 0])
        XCTAssertEqual(Set(codex.quotaGroups.map(\.id)).count, 2)
    }

    func testLiveProxyShapeFeedsProviderCardsQuotaGroupsAndSeparateHistories() throws {
        let snapshot = try decoder().decode(UsageIntelligenceSnapshot.self, from: Data(#"""
        {
          "schema":"coordharness.usage-intelligence.v1",
          "generated_at":"2026-08-30T03:33:30Z",
          "calendar":{"time_zone":"America/New_York","local_date":"2026-08-29"},
          "providers":{
            "claude":{
              "account":{"status":"active","authenticated":true},
              "account_source":{"kind":"claude_code_auth_status","canonical":true},
              "quota_groups":[
                {"key":"account","label":"Account quota","windows":[
                  {"kind":"session","remaining_percent":100,"countdown_seconds":12389},
                  {"kind":"weekly","remaining_percent":25,"countdown_seconds":98789}
                ]},
                {"key":"fable","label":"Fable only","windows":[
                  {"kind":"weekly","name":"Fable","remaining_percent":25,"countdown_seconds":98789}
                ]}
              ],
              "history":{"daily":[{"date":"2026-08-29","total_tokens":154,"api_rate_estimate_nanos":1234567890000}],
                "ever_observed_envelope":{"total_tokens":999}},
              "costs":{"api_rate_estimate":{"amount_nanos":1234567890000,"currency":"USD"}}
            },
            "codex":{
              "account":{"status":"active","authenticated":true},
              "quota_groups":[{"key":"account","label":"Account quota","windows":[
                {"kind":"weekly","remaining_percent":98,"countdown_seconds":595435}
              ]}],
              "history":{"daily":[{"date":"2026-08-29","total_tokens":139}],
                "provider_reported_account":{"daily":[{"date":"2026-08-29","total_tokens":141}],"today_total_tokens":141}},
              "costs":{"api_rate_estimate":{"amount_nanos":2000000000,"currency":"USD"}}
            }
          }
        }
        """#.utf8))

        XCTAssertEqual(UsageProviderCardState.cards(from: snapshot).map(\.id), ["claude", "codex"])
        let summaries = UsageCompactProviderSummary.summaries(from: snapshot)
        let claude = try XCTUnwrap(summaries.first { $0.id == "claude" })
        XCTAssertEqual(claude.session?.resolvedRemainingPercent, 100)
        XCTAssertEqual(claude.weekly?.resolvedRemainingPercent, 25)
        XCTAssertEqual(claude.fable?.resolvedRemainingPercent, 25)
        XCTAssertEqual(claude.retainedUSDEstimateNanos, 1_234_567_890_000)
        let codex = try XCTUnwrap(snapshot.providers["codex"])
        XCTAssertEqual(UsageHistoryPresentation.sources(for: codex).map(\.kind), [.retainedEnvelope, .providerReported])
        XCTAssertEqual(UsageHistoryPresentation.sources(for: codex).map(\.todayTotalTokens), [nil, 141])
        let claudeProvider = try XCTUnwrap(snapshot.providers["claude"])
        XCTAssertEqual(
            UsageDailyCostTrendProjection.make(
                providerID: "claude",
                history: try XCTUnwrap(UsageHistoryPresentation.sources(for: claudeProvider).first),
                costs: claudeProvider.costs
            ).points.first?.nanos,
            1_234_567_890_000
        )
    }

    func testCompactSummaryPrefersAccountGroupWithoutBorrowingModelSession() throws {
        let summaries = UsageCompactProviderSummary.summaries(from: try fixture())
        XCTAssertEqual(summaries.map(\.id), ["claude", "codex"])

        let codex = try XCTUnwrap(summaries.first { $0.id == "codex" })
        XCTAssertEqual(codex.connectionLabel, "Connected")
        XCTAssertEqual(codex.quotaGroupLabel, "Account quota")
        XCTAssertNil(codex.session, "A model-group session must not be attached to account quota")
        XCTAssertEqual(codex.weekly?.resolvedRemainingPercent, 81)
        XCTAssertEqual(codex.todayTokens, 15)
        XCTAssertEqual(codex.retainedUSDEstimateNanos, 9_000_000_000)
    }

    func testNoncanonicalQuotaSnapshotDoesNotClaimDirectConnection() throws {
        func snapshot(authenticated: Bool, canonical: Bool) throws -> UsageIntelligenceSnapshot {
            let payload = #"""
            {
              "schema": "coordharness.usage-intelligence.v1",
              "providers": {
                "claude": {
                  "account": {"authenticated": \#(authenticated)},
                  "quota_source": {
                    "kind": "codexbar_widget_snapshot",
                    "canonical": \#(canonical),
                    "label": "Cached Claude widget snapshot"
                  },
                  "quota_groups": [{
                    "key": "account",
                    "label": "Account quota",
                    "windows": [{"kind": "weekly", "remaining_percent": 64}]
                  }]
                }
              }
            }
            """#
            return try decoder().decode(UsageIntelligenceSnapshot.self, from: Data(payload.utf8))
        }

        let fallback = try snapshot(authenticated: true, canonical: false)
        let fallbackSummary = try XCTUnwrap(UsageCompactProviderSummary.summaries(from: fallback).first)
        XCTAssertEqual(fallbackSummary.connectionLabel, "Fallback snapshot")
        XCTAssertNil(fallbackSummary.connected)
        let status = UsageStatusPresentation.make(from: .success(fallback, at: Date()))
        XCTAssertTrue(status.accessibilityLabel.contains("Claude: Fallback snapshot"))
        XCTAssertFalse(status.accessibilityLabel.contains("Claude: Connected"))
        let peek = UsagePopoverPeekPresentation.make(from: .success(fallback, at: Date()))
        XCTAssertEqual(peek.providers.first?.connectionLabel, "Fallback snapshot")
        XCTAssertNil(peek.providers.first?.connected)
        XCTAssertTrue(peek.accessibilityLabel.contains("Claude: Fallback snapshot"))

        let authoritative = try XCTUnwrap(
            UsageCompactProviderSummary.summaries(
                from: try snapshot(authenticated: true, canonical: true)
            ).first
        )
        XCTAssertEqual(authoritative.connectionLabel, "Connected")
        XCTAssertEqual(authoritative.connected, true)

        let signedOut = try XCTUnwrap(
            UsageCompactProviderSummary.summaries(
                from: try snapshot(authenticated: false, canonical: false)
            ).first
        )
        XCTAssertEqual(signedOut.connectionLabel, "Sign-in required")
        XCTAssertEqual(signedOut.connected, false)
    }

    func testCompactSummaryUsesAggregateUSDAndPrefersByCurrency() throws {
        let data = Data(#"""
        {
          "schema":"coordharness.usage-intelligence.v1",
          "generated_at":"2026-08-26T12:00:00Z",
          "providers":{"claude":{
            "account":{"authenticated":true},
            "history":{"today_total_tokens":42,"daily":[
              {"date":"2026-08-25","api_rate_estimate_nanos":9000000000},
              {"date":"2026-08-26","api_rate_estimate_nanos":2500000000}
            ]},
            "costs":{"api_rate_estimate":{"amount_nanos":8500000000,"currency":"USD","by_currency":{"USD":7500000000}}}
          }}
        }
        """#.utf8)
        let summary = try XCTUnwrap(
            UsageCompactProviderSummary.summaries(
                from: decoder().decode(UsageIntelligenceSnapshot.self, from: data)
            ).first
        )
        XCTAssertEqual(summary.todayTokens, 42)
        XCTAssertEqual(summary.retainedUSDEstimateNanos, 7_500_000_000)

        let nonUSD = Data(String(decoding: data, as: UTF8.self).replacingOccurrences(of: "USD", with: "EUR").utf8)
        XCTAssertNil(
            UsageCompactProviderSummary.summaries(
                from: try decoder().decode(UsageIntelligenceSnapshot.self, from: nonUSD)
            ).first?.retainedUSDEstimateNanos
        )
    }

    func testQuotaSourcePaceAndDailyCostsDecodeWithoutPromotingEstimateSemantics() throws {
        let data = Data(#"""
        {
          "schema": "coordharness.usage-intelligence.v1",
          "generated_at": "2026-08-26T12:00:00Z",
          "providers": {
            "claude": {
              "quota_source": {
                "kind": "codexbar_cli_live",
                "canonical": true,
                "label": "Codex Bar live Claude quota"
              },
              "quota_groups": [{
                "key": "fable-weekly",
                "label": "Fable only",
                "semantics": "provider_rate_limit_group",
                "windows": [{
                  "kind": "weekly",
                  "name": "Fable only",
                  "window_minutes": 10080,
                  "used_percent": 12.5,
                  "remaining_percent": 87.5,
                  "resets_at": "2026-09-01T00:00:00Z",
                  "countdown_seconds": 475200,
                  "pace": {
                    "state": "reserve",
                    "delta_percent": 37,
                    "expected_used_percent": 49.5,
                    "will_last_to_reset": true,
                    "source": "codexbar_local_projection"
                  }
                }]
              }],
              "history": {
                "semantics": "canonical_correctable",
                "daily": [
                  {
                    "date": "2026-08-25",
                    "total_tokens": 100,
                    "api_rate_estimate_nanos": 1500000000,
                    "provider_native_cost_nanos": 1200000000
                  },
                  {
                    "date": "2026-08-26",
                    "total_tokens": 200,
                    "api_rate_estimate_nanos": 2500000000,
                    "provider_native_cost_nanos": 2200000000
                  }
                ]
              }
            }
          }
        }
        """#.utf8)

        let snapshot = try decoder().decode(UsageIntelligenceSnapshot.self, from: data)
        let claude = try XCTUnwrap(snapshot.providers["claude"])
        let source = try XCTUnwrap(claude.quotaSource)
        XCTAssertEqual(source.kind, "codexbar_cli_live", "Internal source identity remains exact")
        XCTAssertEqual(source.displayLabel, "Legacy compatibility source")
        XCTAssertEqual(source.authorityLabel, "authoritative live quota")
        XCTAssertEqual(claude.quotaGroups.first?.safeLabel, "Fable only")
        XCTAssertEqual(claude.quotaGroups.first?.semantics, "provider_rate_limit_group")

        let pace = try XCTUnwrap(claude.quotaGroups.first?.windows.first?.pace)
        XCTAssertEqual(pace.deltaLabel, "37% in reserve")
        XCTAssertEqual(pace.expectedUsedPercent, 49.5)
        XCTAssertEqual(pace.runoutLabel, "Projected to last until reset")
        XCTAssertEqual(pace.provenanceLabel, "Legacy compatibility pace projection")

        XCTAssertEqual(claude.history?.daily.first?.apiRateEstimateNanos, 1_500_000_000)
        XCTAssertEqual(claude.history?.daily.last?.providerNativeCostNanos, 2_200_000_000)
        XCTAssertEqual(UsageProviderCardState.cards(from: snapshot).first?.observedDay?.rawValue, "2026-08-26")
        XCTAssertEqual(UsageFormat.costNanos(2_500_000_000, currency: "usd"), "USD 2.50")
    }

    func testUsagePresentationNeutralizesDonorLabelKindWarningAndErrorWithoutMutatingRawValues() throws {
        let snapshot = try decoder().decode(
            UsageIntelligenceSnapshot.self,
            from: Data(#"""
            {
              "schema": "coordharness.usage-intelligence.v1",
              "providers": {
                "claude": {
                  "source": {
                    "kind": "retained_compatibility",
                    "canonical": false,
                    "label": "Codex Bar retained snapshot",
                    "warning": "CLAUDE_CODEXBAR_WIDGET_STALE; refresh the CodexBar source."
                  },
                  "quota_source": {
                    "kind": "codexbar_widget_snapshot",
                    "canonical": false,
                    "warning": "Codex Bar quota may lag."
                  },
                  "errors": [
                    {"code": "CLAUDE_CODEXBAR_WIDGET_UNAVAILABLE"},
                    {"code": "upstream_unavailable"}
                  ]
                }
              }
            }
            """#.utf8)
        )
        let provider = try XCTUnwrap(snapshot.providers["claude"])
        let source = try XCTUnwrap(provider.source)
        let quotaSource = try XCTUnwrap(provider.quotaSource)

        XCTAssertEqual(source.label, "Codex Bar retained snapshot")
        XCTAssertEqual(source.kind, "retained_compatibility")
        XCTAssertEqual(source.warning, "CLAUDE_CODEXBAR_WIDGET_STALE; refresh the CodexBar source.")
        XCTAssertEqual(provider.errors.map(\.code), [
            "CLAUDE_CODEXBAR_WIDGET_UNAVAILABLE",
            "upstream_unavailable",
        ])

        XCTAssertEqual(source.displayLabel, "Legacy compatibility source")
        XCTAssertEqual(quotaSource.displayLabel, "Legacy compatibility source")
        XCTAssertEqual(
            source.displayWarning,
            "legacy compatibility source; refresh the legacy compatibility source."
        )
        XCTAssertEqual(quotaSource.displayWarning, "legacy compatibility source quota may lag.")
        XCTAssertEqual(provider.errors.map(\.displayLabel), [
            "Legacy compatibility source unavailable",
            "upstream_unavailable",
        ])
        for visible in [
            source.displayLabel,
            quotaSource.displayLabel,
            source.displayWarning ?? "",
            quotaSource.displayWarning ?? "",
            provider.errors.map(\.displayLabel).joined(separator: " "),
            UsagePresentationText.neutralized("CLAUDE_CODEXBAR_PROVIDER_REPORTED"),
        ] {
            XCTAssertFalse(visible.localizedCaseInsensitiveContains("codexbar"), visible)
            XCTAssertFalse(visible.localizedCaseInsensitiveContains("codex bar"), visible)
        }
    }

    func testCostsResetCreditsAndSanitizedSessionsRetainTheirOwnSemantics() throws {
        let codex = try XCTUnwrap(try fixture().providers["codex"])
        XCTAssertEqual(UsageFormat.cost(codex.costs?.providerBilled), "Unknown")
        XCTAssertEqual(UsageFormat.cost(codex.costs?.providerNative), "USD 6.00")
        XCTAssertEqual(UsageFormat.cost(codex.costs?.apiRateEstimate), "USD 9.00")
        XCTAssertEqual(codex.resetCredits.first?.count, 2)
        let session = try XCTUnwrap(codex.activeSessions?.items?.first)
        XCTAssertEqual(session.provider, "codex")
        XCTAssertEqual(session.state, "active")
        XCTAssertNotNil(session.startedAt)
        XCTAssertNotNil(session.lastActivityAt)
        XCTAssertEqual(session.durationSeconds, 3600)
        XCTAssertEqual(session.idleSeconds, 60)
    }

    func testCostCurrencyDecoderIsNormalizedBoundedAndFailClosed() throws {
        let currencies = ["aud", "CAD", "CHF", "EUR", "GBP", "JPY", "NZD", "SEK", "USD", "usd"]
            .enumerated()
            .map { #""\#($0.element)":\#($0.offset + 1)"# }
            .joined(separator: ",")
        let data = Data(#"{"schema":"coordharness.usage-intelligence.v1","providers":{"codex":{"costs":{"api_rate_estimate":{"amount_nanos":1000000000,"currency":"usd","by_currency":{\#(currencies)}},"provider_native":{"amount_nanos":1000000000,"currency":"not-a-code"}}}}}"#.utf8)
        let costs = try XCTUnwrap(try decoder().decode(UsageIntelligenceSnapshot.self, from: data).providers["codex"]?.costs)
        XCTAssertEqual(costs.apiRateEstimate?.currency, "USD")
        XCTAssertEqual(costs.apiRateEstimate?.byCurrency?.count, 8)
        XCTAssertNil(costs.providerNative?.currency)
        XCTAssertEqual(UsageFormat.cost(costs.providerNative), "units 1.00")
        XCTAssertFalse(UsageFormat.cost(costs.providerNative).contains("USD"))
    }

    func testCurrencyFormattingGroupsThousandsAndRoundsNanosDeterministically() {
        XCTAssertEqual(UsageFormat.costNanos(1_234_567_890_000, currency: "USD"), "USD 1,234.57")
        XCTAssertEqual(UsageFormat.costNanos(-1_234_567_890_000, currency: "USD"), "USD -1,234.57")
        XCTAssertEqual(UsageFormat.costNanos(999_995_000_000, currency: "USD"), "USD 1,000.00")
        XCTAssertEqual(UsageFormat.costNanos(12_340_000_000, currency: nil), "units 12.34")
    }

    func testHistoryPresentationKeepsRetainedAndProviderReportedSourcesSeparateWithProvenance() throws {
        let snapshot = try fixture()
        let codex = try XCTUnwrap(snapshot.providers["codex"])
        let codexSources = UsageHistoryPresentation.sources(for: codex)
        XCTAssertEqual(codexSources.map(\.kind), [.canonical, .providerReported])
        XCTAssertEqual(codexSources.map(\.todayTotalTokens), [15, 700])
        XCTAssertTrue(codexSources.allSatisfy { !$0.provenance.isEmpty })

        let claude = try XCTUnwrap(snapshot.providers["claude"])
        let claudeSources = UsageHistoryPresentation.sources(for: claude)
        XCTAssertEqual(claudeSources.map(\.kind), [.retainedEnvelope, .providerReported])
        XCTAssertEqual(claudeSources.map(\.todayTotalTokens), [42, 333])
        XCTAssertEqual(claudeSources.first?.label, "Retained local envelope")
        XCTAssertEqual(claudeSources.last?.label, "Provider-reported account")

        let fallbackData = Data(#"""
        {
          "schema": "coordharness.usage-intelligence.v1",
          "providers": {
            "claude": {
              "source": {"canonical": false},
              "history": {
                "today_total_tokens": 42,
                "semantics": "ever_observed_envelope"
              }
            }
          }
        }
        """#.utf8)
        let fallbackProvider = try XCTUnwrap(
            decoder().decode(UsageIntelligenceSnapshot.self, from: fallbackData).providers["claude"]
        )
        let fallbackSources = UsageHistoryPresentation.sources(for: fallbackProvider)
        XCTAssertEqual(fallbackSources.count, 1)
        let fallback = try XCTUnwrap(fallbackSources.first)
        XCTAssertEqual(fallback.kind, .retainedEnvelope)
        XCTAssertEqual(fallback.todayTotalTokens, 42)
    }

    func testBreakdownsDecodeRankAndRetainCoverageSemantics() throws {
        let codex = try XCTUnwrap(try fixture().providers["codex"])
        let models = try XCTUnwrap(codex.breakdowns?.models)
        XCTAssertEqual(models.status, "available")
        XCTAssertEqual(models.semantics, "canonical_ledger_coverage")
        XCTAssertEqual(models.canonical, true)
        XCTAssertEqual(models.coverageStart?.rawValue, "2026-08-01")
        XCTAssertEqual(models.coverageEnd?.rawValue, "2026-08-26")
        XCTAssertNotNil(models.observedAt)
        XCTAssertEqual(models.omittedCount, 1)
        XCTAssertEqual(models.rankedItems.map(\.key), ["gpt-5.5", "gpt-5.6-sol"])
        XCTAssertEqual(models.rankedItems.first?.totalTokens, 700)
        XCTAssertEqual(models.rankedItems.first?.apiRateEstimateNanos, 4_000_000_000)

        let projects = try XCTUnwrap(codex.breakdowns?.projects)
        XCTAssertEqual(projects.semantics, "sanitized_project_coverage")
        XCTAssertEqual(projects.rankedItems.map(\.sanitizedLabel), ["Alpha", "Beta"])
        XCTAssertEqual(projects.rankedItems.first?.sanitizedOpaqueKey, "prj_opaque_01")
        XCTAssertEqual(projects.rankedItems.first?.topModel, "gpt-5.5")
        XCTAssertEqual(projects.rankedItems.first?.totalTokens, 900, "Total is the declared coverage total")
    }

    func testLiveShapedCoverageDaysDecodeWhileObservedInstantsRemainDates() throws {
        let snapshot = try fixture(named: "usage-dashboard-live-v1")
        let codex = try XCTUnwrap(snapshot.providers["codex"])
        let models = try XCTUnwrap(codex.breakdowns?.models)
        let projects = try XCTUnwrap(codex.breakdowns?.projects)

        XCTAssertEqual(models.coverageStart?.rawValue, "2026-02-27")
        XCTAssertEqual(models.coverageEnd?.rawValue, "2026-08-26")
        XCTAssertEqual(projects.coverageStart?.rawValue, "2026-02-27")
        XCTAssertEqual(projects.coverageEnd?.rawValue, "2026-08-26")
        XCTAssertNotNil(models.observedAt)
        XCTAssertNotNil(projects.observedAt)
        XCTAssertNotNil(codex.liveObservedAt)
        XCTAssertNotNil(snapshot.generatedAt)
    }

    func testCalendarDayValidationRejectsTimestampsAndImpossibleDates() {
        XCTAssertEqual(UsageCalendarDay("2024-02-29")?.rawValue, "2024-02-29")
        XCTAssertNil(UsageCalendarDay("2026-02-29"))
        XCTAssertNil(UsageCalendarDay("2026-02-30"))
        XCTAssertNil(UsageCalendarDay("2026-8-26"))
        XCTAssertNil(UsageCalendarDay("2026-08-26T00:00:00Z"))
    }

    func testUsageDashboardLayoutStacksProvidersAt500AndAdaptsAtRegularAndWideWidths() {
        let narrow = UsageDashboardLayout.plan(forWidth: 500)
        XCTAssertEqual(narrow.widthClass, .compact)
        XCTAssertEqual(narrow.providerColumnCount, 1)
        XCTAssertEqual(narrow.historyColumnCount, 1)
        XCTAssertEqual(narrow.metricColumnCount, 2)
        XCTAssertEqual(narrow.costColumnCount, 1)

        let regular = UsageDashboardLayout.plan(forWidth: 900)
        XCTAssertEqual(regular.widthClass, .regular)
        XCTAssertEqual(regular.providerColumnCount, 1)
        XCTAssertEqual(regular.historyColumnCount, 1)

        let wide = UsageDashboardLayout.plan(forWidth: 1_440)
        XCTAssertEqual(wide.widthClass, .wide)
        XCTAssertEqual(wide.providerColumnCount, 1)
        XCTAssertEqual(wide.historyColumnCount, 1)
        XCTAssertEqual(wide.metricColumnCount, 2)
        XCTAssertEqual(wide.costColumnCount, 1)

        XCTAssertEqual(UsageDashboardLayout.plan(forWidth: 1_440, forceCompact: true).widthClass, .compact)
        XCTAssertEqual(UsageDashboardLayout.plan(forWidth: 1_440, forceCompact: true).historyColumnCount, 1)
    }

    func testModelBreakdownCostsUseTheirSeparateCurrencies() throws {
        let providerNative = UsageFormat.costNanos(1_500_000_000, currency: nil)
        let apiEstimate = UsageFormat.costNanos(2_500_000_000, currency: "USD")

        XCTAssertEqual(providerNative, "units 1.50")
        XCTAssertEqual(apiEstimate, "USD 2.50")
        XCTAssertFalse(providerNative.contains("USD"))
        XCTAssertTrue(apiEstimate.contains("USD"))
        XCTAssertEqual(UsageFormat.costNanos(nil, currency: "USD"), "Unknown")

        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(contentsOf: projectRoot.appendingPathComponent("apps/Shared/Sources/UsageDashboardContent.swift"), encoding: .utf8)
        XCTAssertTrue(source.contains("UsageFormat.costNanos(item.providerNativeCostNanos"))
        XCTAssertTrue(source.contains("UsageFormat.costNanos(item.apiRateEstimateNanos"))
    }

    func testBreakdownListsAreBoundedAndPathLikeProjectIdentityIsHidden() throws {
        let items: [[String: Any]] = (0..<30).map {
            ["key": "model-\($0)", "label": "Model \($0)", "total_tokens": $0]
        }
        let payload: [String: Any] = [
            "schema": UsageIntelligenceContract.identifier,
            "providers": [
                "codex": [
                    "breakdowns": [
                        "models": ["omitted_count": 2, "items": items],
                        "projects": [
                            "items": [[
                                "key": "/private/secret",
                                "label": "/redacted/project",
                                "path": "/must/not/render",
                                "total_tokens": 1,
                            ]],
                        ],
                    ],
                ],
            ],
        ]
        let snapshot = try decoder().decode(
            UsageIntelligenceSnapshot.self,
            from: JSONSerialization.data(withJSONObject: payload)
        )
        let models = try XCTUnwrap(snapshot.providers["codex"]?.breakdowns?.models)
        XCTAssertEqual(models.items.count, UsageBreakdown<UsageModelBreakdownItem>.itemLimit)
        XCTAssertEqual(models.omittedCount, 7)
        XCTAssertEqual(models.rankedItems.first?.totalTokens, 29)

        let project = try XCTUnwrap(snapshot.providers["codex"]?.breakdowns?.projects?.items.first)
        XCTAssertEqual(project.sanitizedLabel, "Project")
        XCTAssertEqual(project.sanitizedOpaqueKey, "opaque key unavailable")
    }

    func testUnknownAdditiveFieldsDecodeAndUnsupportedContractFailsClosed() throws {
        let additive = Data(#"{"schema":"coordharness.usage-intelligence.v1","providers":{"codex":{"future":true}},"future_top":1}"#.utf8)
        let snapshot = try decoder().decode(UsageIntelligenceSnapshot.self, from: additive)
        XCTAssertTrue(snapshot.providers["codex"]?.windows.isEmpty == true)

        let unsupported = Data(#"{"schema":"coordharness.usage-intelligence.v2","providers":{}}"#.utf8)
        XCTAssertThrowsError(try decoder().decode(UsageIntelligenceSnapshot.self, from: unsupported)) { error in
            XCTAssertEqual(error as? UsageIntelligenceError, .unsupportedContract("coordharness.usage-intelligence.v2"))
        }
    }

    func testLastGoodStateBecomesStaleAfterGrace() throws {
        let now = Date(timeIntervalSince1970: 1_000)
        let good = UsageDashboardState.success(try fixture(), at: now)
        let recent = UsageDashboardState.preserving(good, error: SnapshotError.serverStatus(503), at: now.addingTimeInterval(30), grace: 60)
        let stale = UsageDashboardState.preserving(recent, error: SnapshotError.serverStatus(503), at: now.addingTimeInterval(90), grace: 60)
        XCTAssertNotNil(recent.snapshot)
        XCTAssertTrue(recent.refreshing)
        XCTAssertFalse(recent.stale)
        XCTAssertNil(recent.error)
        XCTAssertNotNil(stale.snapshot)
        XCTAssertTrue(stale.stale)
        XCTAssertEqual(stale.error, "The server returned HTTP 503.")
    }

    func testValidEmptyErrorEnvelopeDoesNotReplaceLastGood() throws {
        let now = Date(timeIntervalSince1970: 1_000)
        let good = UsageDashboardState.success(try fixture(), at: now)
        let empty = try decoder().decode(
            UsageIntelligenceSnapshot.self,
            from: Data(#"{"schema":"coordharness.usage-intelligence.v1","refresh":{"state":"error"},"providers":{"claude":{"account":{"authenticated":true}}},"errors":[{"code":"upstream_unavailable"}]}"#.utf8)
        )

        let preserved = UsageDashboardState.accepting(
            empty,
            preserving: good,
            at: now.addingTimeInterval(30),
            grace: 60
        )
        XCTAssertEqual(preserved.snapshot, good.snapshot)
        XCTAssertTrue(preserved.refreshing)
        XCTAssertNil(preserved.error)

        let expired = UsageDashboardState.accepting(
            empty,
            preserving: preserved,
            at: now.addingTimeInterval(90),
            grace: 60
        )
        XCTAssertEqual(expired.snapshot, good.snapshot)
        XCTAssertTrue(expired.stale)
        XCTAssertEqual(expired.error, "Usage refresh returned no meaningful provider data.")
    }

    func testProviderScopedClaudeQuotaRetentionIsBoundedAndCodexAdvances() throws {
        func decode(_ json: String) throws -> UsageIntelligenceSnapshot {
            try decoder().decode(UsageIntelligenceSnapshot.self, from: Data(json.utf8))
        }
        let now = Date(timeIntervalSince1970: 1_000)
        let good = try decode(#"""
        {"schema":"coordharness.usage-intelligence.v1","providers":{
          "claude":{
            "account":{"authenticated":true,"status":"authenticated"},
            "quota_source":{"kind":"live","canonical":true,"label":"Claude live"},
            "windows":[{"kind":"session","remaining_percent":70,"resets_at":"1970-01-01T00:33:20Z"}],
            "quota_groups":[{"key":"account","label":"Account quota","windows":[
              {"kind":"session","remaining_percent":70,"resets_at":"1970-01-01T00:33:20Z"}
            ]}]
          },
          "codex":{
            "account":{"authenticated":true},
            "quota_groups":[{"key":"account","windows":[
              {"kind":"weekly","remaining_percent":80,"resets_at":"1970-01-01T00:50:00Z"}
            ]}]
          }
        }}
        """#)
        let partial = try decode(#"""
        {"schema":"coordharness.usage-intelligence.v1","refresh":{"state":"fresh"},"providers":{
          "claude":{"account":{"authenticated":true,"status":"authenticated"},"quota_groups":[]},
          "codex":{
            "account":{"authenticated":true},
            "quota_groups":[{"key":"account","windows":[
              {"kind":"weekly","remaining_percent":55,"resets_at":"1970-01-01T00:50:00Z"}
            ]}]
          }
        }}
        """#)

        let retained = UsageDashboardState.accepting(
            partial,
            preserving: .success(good, at: now),
            at: now,
            grace: 60
        )
        let claude = try XCTUnwrap(retained.snapshot?.providers["claude"])
        let codex = try XCTUnwrap(retained.snapshot?.providers["codex"])
        XCTAssertEqual(claude.quotaGroups.first?.windows.first?.remainingPercent, 70)
        XCTAssertEqual(codex.quotaGroups.first?.windows.first?.remainingPercent, 55)
        XCTAssertEqual(claude.liveObservationState, "stale_last_good_no_current_windows")
        XCTAssertEqual(claude.quotaSource?.canonical, false)
        XCTAssertTrue(claude.quotaSource?.warning?.contains("bounded last-good") == true)
        XCTAssertTrue(retained.stale)
        XCTAssertTrue(retained.snapshot?.errors.contains {
            $0.code == "claude_quota_windows_retained_last_good"
        } == true)

        let signedOut = try decode(#"""
        {"schema":"coordharness.usage-intelligence.v1","providers":{
          "claude":{"account":{"authenticated":false,"status":"signed_out"}},
          "codex":{"account":{"authenticated":true}}
        }}
        """#)
        let cleared = UsageDashboardState.accepting(
            signedOut, preserving: retained, at: now, grace: 60
        )
        XCTAssertTrue(cleared.snapshot?.providers["claude"]?.quotaGroups.isEmpty == true)

        let expired = try decode(#"""
        {"schema":"coordharness.usage-intelligence.v1","providers":{
          "claude":{
            "account":{"authenticated":true,"status":"authenticated"},
            "live_observation_state":"quota_observation_expired"
          },
          "codex":{"account":{"authenticated":true}}
        }}
        """#)
        let expiredState = UsageDashboardState.accepting(
            expired, preserving: .success(good, at: now), at: now, grace: 60
        )
        XCTAssertTrue(expiredState.snapshot?.providers["claude"]?.quotaGroups.isEmpty == true)

        let missingReset = try decode(#"""
        {"schema":"coordharness.usage-intelligence.v1","providers":{
          "claude":{
            "account":{"authenticated":true},
            "quota_groups":[{"key":"account","windows":[
              {"kind":"session","remaining_percent":70}
            ]}]
          },
          "codex":{"account":{"authenticated":true}}
        }}
        """#)
        let missingResetState = UsageDashboardState.accepting(
            partial,
            preserving: .success(missingReset, at: now),
            at: now,
            grace: 60
        )
        XCTAssertTrue(
            missingResetState.snapshot?.providers["claude"]?.quotaGroups.isEmpty == true
        )
    }

    func testRefreshStateStaleAndStatusAccessibilityAreExplicit() throws {
        let data = Data(#"{"schema":"coordharness.usage-intelligence.v1","refresh":{"state":"stale"},"providers":{"claude":{"account":{"authenticated":true}}}}"#.utf8)
        let snapshot = try decoder().decode(UsageIntelligenceSnapshot.self, from: data)
        XCTAssertTrue(snapshot.isProducerStale)
        let status = UsageStatusPresentation.make(from: .success(snapshot, at: Date()))
        XCTAssertTrue(status.stale)
        XCTAssertTrue(status.accessibilityLabel.hasPrefix("Provider quota stale."))
        XCTAssertTrue(status.accessibilityLabel.contains("Claude: Connected"))
        XCTAssertTrue(status.accessibilityLabel.contains("quota unavailable"))
    }

    func testProviderLevelStaleLastGoodMarksFreshEnvelopeStale() throws {
        let data = Data(#"{"schema":"coordharness.usage-intelligence.v1","refresh":{"state":"fresh"},"providers":{"claude":{"account":{"authenticated":true},"live_observation_state":"stale_last_good"}}}"#.utf8)
        let snapshot = try decoder().decode(UsageIntelligenceSnapshot.self, from: data)

        XCTAssertEqual(snapshot.providers["claude"]?.liveObservationState, "stale_last_good")
        XCTAssertTrue(snapshot.providers["claude"]?.hasStaleQuotaObservation == true)
        XCTAssertTrue(snapshot.isProducerStale)
        let status = UsageStatusPresentation.make(from: .success(snapshot, at: Date()))
        XCTAssertTrue(status.stale)
        XCTAssertTrue(status.accessibilityLabel.hasPrefix("Provider quota stale."))
    }

    func testStatusModesMigrateLegacyValuesAndActiveTaskLabelIsBounded() {
        XCTAssertEqual(UsageStatusMode.resolve(nil), .bars)
        XCTAssertEqual(UsageStatusMode.resolve("eta_ring"), .rings)
        XCTAssertEqual(UsageStatusMode.resolve("count"), .minimal)
        XCTAssertEqual(
            UsageStatusTaskPresentation(title: "Task", percent: 122.4, eta: " 2 h ").compactLabel,
            "100% · 2 h"
        )
        XCTAssertEqual(
            UsageStatusTaskPresentation(title: "Task", percent: nil, eta: "—").compactLabel,
            "Active"
        )
    }

    func testAutoQuotaModeUsesLowestRealWindowAndNeverInventsCodexSession() throws {
        let snapshot = try decoder().decode(UsageIntelligenceSnapshot.self, from: Data(#"""
        {"schema":"coordharness.usage-intelligence.v1","providers":{
          "claude":{"quota_groups":[{"key":"account","windows":[
            {"kind":"session","remaining_percent":42},{"kind":"weekly","remaining_percent":27}]}]},
          "codex":{"quota_groups":[{"key":"account","windows":[
            {"kind":"weekly","remaining_percent":34}]}]}
        }}
        """#.utf8))
        let state = UsageDashboardState.success(snapshot, at: Date())
        let auto = UsageStatusPresentation.make(from: state, metricMode: "auto", sessionThreshold: 50)
        XCTAssertEqual(auto.selections.map { $0.window?.resolvedRemainingPercent }, [27, 34])
        XCTAssertEqual(auto.selections.map(\.label), ["Weekly", "Weekly"])
        let weekly = UsageStatusPresentation.make(from: state, metricMode: "weekly", sessionThreshold: 50)
        XCTAssertEqual(weekly.selections.map { $0.window?.resolvedRemainingPercent }, [27, 34])

        let lowSessionSnapshot = try decoder().decode(UsageIntelligenceSnapshot.self, from: Data(#"""
        {"schema":"coordharness.usage-intelligence.v1","providers":{
          "claude":{"quota_groups":[{"key":"account","windows":[
            {"kind":"session","remaining_percent":8},{"kind":"weekly","remaining_percent":27}]}]}
        }}
        """#.utf8))
        let lowSession = UsageStatusPresentation.make(
            from: .success(lowSessionSnapshot, at: Date()),
            metricMode: "auto",
            sessionThreshold: 0
        )
        XCTAssertEqual(lowSession.selections.first?.window?.resolvedRemainingPercent, 8)
        XCTAssertEqual(lowSession.selections.first?.label, "Session")
    }

    func testStatusTaskSelectionCollapsesWidthWhenRowsStopRunning() throws {
        let rows = try JSONDecoder().decode([Row].self, from: Data(#"""
        [
          {"display":"Active","status":"RUNNING","live":true,"paused":false,"stale":false,"pct":42},
          {"display":"Paused","status":"RUNNING","live":true,"paused":true,"stale":false,"pct":99},
          {"display":"Not live","status":"RUNNING","live":false,"paused":false,"stale":false,"pct":98},
          {"display":"Finished","status":"DONE","live":true,"paused":false,"stale":false,"pct":97}
        ]
        """#.utf8))

        let active = try XCTUnwrap(StatusItemTaskSelection.topRow(from: rows))
        XCTAssertEqual(active.title, "Active")
        XCTAssertEqual(active.effectivePct, 42)
        XCTAssertEqual(
            UsageStatusLayout.imageWidth(mode: .bars, hasActiveTask: true),
            116
        )

        let idle = StatusItemTaskSelection.topRow(from: Array(rows.dropFirst()))
        XCTAssertNil(idle)
        XCTAssertEqual(
            UsageStatusLayout.imageWidth(mode: .bars, hasActiveTask: idle != nil),
            56,
            "The status image must collapse after active work stops"
        )
    }


    func testStatusBarGeometryStaysCompactAndNonOverlapping() {
        XCTAssertEqual(UsageStatusLayout.baseWidth(mode: .bars), 56)
        XCTAssertEqual(UsageStatusLayout.imageHeight, 22)
        XCTAssertEqual(UsageStatusLayout.activeTaskWidth, 60)
        XCTAssertGreaterThan(UsageStatusLayout.providerIconSize, 10)
        XCTAssertEqual(UsageStatusLayout.quotaBarWidth, 20)
        XCTAssertLessThanOrEqual(
            UsageStatusLayout.quotaBarX + UsageStatusLayout.quotaBarWidth,
            UsageStatusLayout.quotaPercentX
        )
        XCTAssertEqual(
            UsageStatusLayout.quotaPercentX - (UsageStatusLayout.quotaBarX + UsageStatusLayout.quotaBarWidth),
            1
        )
        XCTAssertLessThanOrEqual(UsageStatusLayout.quotaPercentX + UsageStatusLayout.percentWidth, 56)
    }

    func testStatusBadgeRendererPaletteAndWidthContract() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let renderer = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/UI/RingRenderer.swift"),
            encoding: .utf8
        )
        XCTAssertTrue(renderer.contains("static func quotaPercent"))
        XCTAssertTrue(renderer.contains("return \"\\(Int(min(max(remaining, 0), 100).rounded()))\""))
        XCTAssertFalse(renderer.contains("return \"\\(Int(min(max(remaining, 0), 100).rounded()))%\""))
        XCTAssertTrue(renderer.contains("warningMarkerColor: warningMarkerColor(for: palette)"))
        XCTAssertTrue(renderer.contains("palette == .colored ? .white : .systemRed"))
        XCTAssertTrue(renderer.contains("Tokens.Color.claudeOrange"))
        XCTAssertLessThanOrEqual(UsageStatusLayout.quotaPercentX + UsageStatusLayout.percentWidth, UsageStatusLayout.baseWidth(mode: .bars))
    }

    func testCompactQuotaTimingUsesWindowResetAndPaceRunoutOnly() throws {
        let snapshot = try decoder().decode(UsageIntelligenceSnapshot.self, from: Data(#"""
        {
          "schema":"coordharness.usage-intelligence.v1",
          "providers":{"claude":{
            "quota_groups":[
              {"key":"account","label":"Account quota","windows":[
                {"kind":"session","remaining_percent":70,"countdown_seconds":7200,
                 "pace":{"seconds_to_exhaustion":2700}},
                {"kind":"weekly","remaining_percent":60,"countdown_seconds":475200,
                 "pace":{"state":"reserve"}}
              ]},
              {"key":"fable","label":"Fable quota","windows":[
                {"kind":"weekly","name":"Fable","remaining_percent":55,"countdown_seconds":86400,
                 "pace":{"seconds_to_exhaustion":3600}}
              ]},
              {"key":"bounded","label":"Bounded quota","windows":[
                {"kind":"daily","remaining_percent":50,"countdown_seconds":3600,
                 "pace":{"will_last_to_reset":true,"seconds_to_exhaustion":1200}},
                {"kind":"monthly","remaining_percent":40,"countdown_seconds":3600,
                 "pace":{"will_last_to_reset":false,"seconds_to_exhaustion":7200}}
              ]}
            ]
          }}
        }
        """#.utf8))

        let peek = UsagePopoverPeekPresentation.make(from: .success(snapshot, at: Date()))
        let claude = try XCTUnwrap(peek.providers.first)
        XCTAssertEqual(claude.sessionTiming.resetLabel, "2h")
        XCTAssertEqual(claude.sessionTiming.runoutLabel, "45m")
        XCTAssertEqual(claude.weeklyTiming.resetLabel, "5d 12h")
        XCTAssertEqual(claude.weeklyTiming.runoutLabel, "—", "Group-level runout must not be promoted into a window")
        XCTAssertTrue(claude.hasFable)
        XCTAssertEqual(claude.fableRemainingPercent, 55)
        XCTAssertEqual(claude.fableTiming.resetLabel, "1d")
        XCTAssertEqual(claude.fableTiming.runoutLabel, "1h")
        XCTAssertTrue(peek.accessibilityLabel.contains("run-out unavailable"))

        let bounded = try XCTUnwrap(snapshot.providers["claude"]?.quotaGroups.first { $0.key == "bounded" })
        XCTAssertEqual(UsageCompactQuotaTiming.make(from: bounded.windows[0]).runoutLabel, "—")
        XCTAssertEqual(UsageCompactQuotaTiming.make(from: bounded.windows[1]).runoutLabel, "—")
    }

    func testDailyTrendProjectionKeepsRetainedAndProviderReportedSourcesSeparate() throws {
        let claude = try XCTUnwrap(try fixture().providers["claude"])
        let sources = UsageHistoryPresentation.sources(for: claude)
        let retained = try XCTUnwrap(sources.first { $0.kind == .retainedEnvelope })
        let reported = try XCTUnwrap(sources.first { $0.kind == .providerReported })
        let retainedTrend = UsageDailyTrendProjection.make(providerID: "Claude", history: retained)
        let reportedTrend = UsageDailyTrendProjection.make(providerID: "Claude", history: reported)

        XCTAssertEqual(retainedTrend.providerID, "claude")
        XCTAssertEqual(retainedTrend.sourceKind, .retainedEnvelope)
        XCTAssertEqual(retainedTrend.points, [UsageDailyTrendPoint(day: "2026-08-26", totalTokens: 99)])
        XCTAssertEqual(reportedTrend.sourceKind, .providerReported)
        XCTAssertEqual(reportedTrend.points, [UsageDailyTrendPoint(day: "2026-08-26", totalTokens: 333)])
        XCTAssertNotEqual(retainedTrend.points, reportedTrend.points)
    }

    func testDailyCostTrendProjectionPrefersAPIEstimateAndKeepsSourcesSeparate() throws {
        let claude = try XCTUnwrap(try fixture().providers["claude"])
        let sources = UsageHistoryPresentation.sources(for: claude)
        let retained = try XCTUnwrap(sources.first { $0.kind == .retainedEnvelope })
        let reported = try XCTUnwrap(sources.first { $0.kind == .providerReported })
        let retainedCost = UsageDailyCostTrendProjection.make(
            providerID: "Claude", history: retained, costs: claude.costs
        )
        let reportedCost = UsageDailyCostTrendProjection.make(
            providerID: "Claude", history: reported, costs: claude.costs
        )

        XCTAssertEqual(retainedCost.providerID, "claude")
        XCTAssertEqual(retainedCost.sourceKind, .retainedEnvelope)
        XCTAssertEqual(
            retainedCost.points,
            [UsageDailyCostTrendPoint(day: "2026-08-26", nanos: 1_500_000_000, costKind: "API-rate estimate", currency: "USD")]
        )
        XCTAssertEqual(reportedCost.sourceKind, .providerReported)
        XCTAssertEqual(
            reportedCost.points,
            [UsageDailyCostTrendPoint(day: "2026-08-26", nanos: 2_200_000_000, costKind: "provider-native", currency: nil)]
        )
        XCTAssertNotEqual(retainedCost.points, reportedCost.points)
    }

    func testEarnedResetInventoryNeverClaimsCurrentResetEligibility() throws {
        let snapshot = try decoder().decode(UsageIntelligenceSnapshot.self, from: Data(#"""
        {
          "schema":"coordharness.usage-intelligence.v1",
          "providers":{"codex":{"reset_credits":[{
            "status":"available",
            "count":1,
            "semantics":"earned_credit_inventory_not_current_reset_eligibility"
          }]}}
        }
        """#.utf8))
        let credit = try XCTUnwrap(snapshot.providers["codex"]?.resetCredits.first)
        XCTAssertTrue(credit.isEarnedInventory)
        XCTAssertEqual(credit.sourceHonestLabel, "Earned reset inventory: 1")

        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/Shared/Sources/UsageDashboardContent.swift"),
            encoding: .utf8
        )
        XCTAssertTrue(source.contains("Current reset eligibility unverified"))
        XCTAssertFalse(source.localizedCaseInsensitiveContains("reset credits available"))
    }

    func testCompactUsageVisualAndGraphSourceContracts() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let popoverSource = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/UI/ContentStackAndRows.swift"),
            encoding: .utf8
        )
        let rendererSource = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/UI/RingRenderer.swift"),
            encoding: .utf8
        )
        let dashboardSource = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/Shared/Sources/UsageDashboardContent.swift"),
            encoding: .utf8
        )

        let peekStart = try XCTUnwrap(popoverSource.range(of: "final class UsagePeekRow"))
        let peekEnd = try XCTUnwrap(popoverSource.range(of: "final class FooterView"))
        let peek = String(popoverSource[peekStart.lowerBound..<peekEnd.lowerBound])
        XCTAssertTrue(peek.contains("layer?.backgroundColor = NSColor.clear.cgColor"))
        XCTAssertTrue(peek.contains("ProviderMenuMark.image(for: provider.id)?.copy()"))
        XCTAssertFalse(peek.contains("UI.label(provider.displayName"), "Provider identity must remain logo-only in the compact dropdown")
        for (compact, full) in [("S", "Session"), ("W", "Weekly"), ("F", "Fable")] {
            XCTAssertTrue(peek.contains("label: \"\(compact)\", fullLabel: \"\(full)\""), full)
        }
        XCTAssertTrue(peek.contains("addCollapsedQuotaSummaries(presentation)"))
        XCTAssertTrue(peek.contains("timing.resetLabel"))
        XCTAssertTrue(peek.contains("timing.runoutLabel"))
        XCTAssertFalse(peek.contains("UI.label(\"Cost\""))
        XCTAssertTrue(peek.contains("weight: .regular"))
        XCTAssertTrue(peek.contains("costValue"))
        XCTAssertTrue(peek.contains("visibleState(provider, freshness: presentation.freshness)"))
        XCTAssertTrue(peek.contains("if let state = visibleState"))
        XCTAssertTrue(peek.contains("if provider.connected == true { return nil }"))
        XCTAssertTrue(peek.contains("providerY + (Self.quotaRowHeight - 18) / 2"))
        XCTAssertTrue(peek.contains("srgbRed: 0.95, green: 0.47, blue: 0.24"))
        XCTAssertTrue(peek.contains("srgbRed: 0.64, green: 0.43, blue: 0.96"))
        XCTAssertFalse(peek.contains("Today "))
        XCTAssertFalse(peek.contains("separatorColor"), "Usage must flow into the panel without a hard divider")

        XCTAssertTrue(dashboardSource.contains("private enum UsageProviderVisualStyle"))
        XCTAssertTrue(dashboardSource.contains("if let connectionNotice"))

        XCTAssertTrue(rendererSource.contains("Tokens.Color.claudeOrange"), "Claude compact color uses the dedicated warm brand orange")
        XCTAssertTrue(rendererSource.contains("green: 0.40, blue: 0.96"), "Codex compact color is purple")
        XCTAssertTrue(rendererSource.contains("compositingOperation = .sourceAtop"), "Real provider marks are visibly tinted")

        let metricsStart = try XCTUnwrap(dashboardSource.range(of: "private var compactMetrics"))
        let metricsEnd = try XCTUnwrap(dashboardSource.range(of: "private var quotaSection"))
        let compactMetrics = String(dashboardSource[metricsStart.lowerBound..<metricsEnd.lowerBound])
        XCTAssertTrue(compactMetrics.contains("label: \"Today Est.\""))
        XCTAssertTrue(compactMetrics.contains("label: \"Cost\""))
        XCTAssertTrue(compactMetrics.contains("latestDailyCost.nanos"))
        XCTAssertFalse(compactMetrics.localizedCaseInsensitiveContains("all time"))
        XCTAssertTrue(dashboardSource.contains("UsageDailyCostTrendProjection.make"))
        XCTAssertTrue(dashboardSource.contains("Daily estimated cost"))
        XCTAssertTrue(dashboardSource.contains("missing days are not plotted as zero"))
    }

    func testPopoverPeekKeepsProvidersFixedAndTruthExplicit() throws {
        let fixtureSnapshot = try fixture()
        let snapshot = UsageIntelligenceSnapshot(
            schema: fixtureSnapshot.schema,
            generatedAt: fixtureSnapshot.generatedAt,
            staleAfter: nil,
            refresh: nil,
            providers: fixtureSnapshot.providers,
            errors: fixtureSnapshot.errors
        )
        let live = UsagePopoverPeekPresentation.make(from: .success(snapshot, at: Date()))

        XCTAssertEqual(live.freshness, .live)
        XCTAssertEqual(live.providers.map(\.id), ["claude", "codex"])
        let codex = try XCTUnwrap(live.providers.last)
        XCTAssertNil(codex.sessionRemainingPercent, "Account quota must not borrow the Spark session window")
        XCTAssertEqual(codex.weeklyRemainingPercent, 81)
        XCTAssertEqual(codex.todayTokens, 15)
        XCTAssertEqual(codex.retainedUSDEstimateNanos, 9_000_000_000)
        XCTAssertTrue(live.accessibilityLabel.contains("session unavailable, reset unavailable; run-out unavailable"))
        XCTAssertTrue(live.accessibilityLabel.contains("weekly 81 percent remaining, resets in 5d 12h; run-out unavailable"))

        var staleState = UsageDashboardState.success(snapshot, at: Date())
        staleState.stale = true
        let stale = UsagePopoverPeekPresentation.make(from: staleState)
        XCTAssertEqual(stale.freshness, .stale)
        XCTAssertTrue(stale.accessibilityLabel.hasPrefix("Provider usage stale."))

        let unavailable = UsagePopoverPeekPresentation.make(from: UsageDashboardState())
        XCTAssertEqual(unavailable.freshness, .unavailable)
        XCTAssertEqual(unavailable.providers.map(\.connectionLabel), ["Unavailable", "Unavailable"])

        let signedOut = try decoder().decode(UsageIntelligenceSnapshot.self, from: Data(#"""
        {
          "schema": "coordharness.usage-intelligence.v1",
          "providers": {"claude": {"account": {"authenticated": false}}}
        }
        """#.utf8))
        let signedOutPeek = UsagePopoverPeekPresentation.make(from: .success(signedOut, at: Date()))
        XCTAssertEqual(signedOutPeek.providers.map(\.id), ["claude", "codex"])
        XCTAssertEqual(signedOutPeek.providers[0].connectionLabel, "Sign-in required")
        XCTAssertEqual(signedOutPeek.providers[1].connectionLabel, "Unavailable")
    }

    func testMenuMarksAreVisibleTransparentImages() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        for identity in ["claude", "codex"] {
            let url = projectRoot.appendingPathComponent("apps/brand/Assets/\(identity)-menu.png")
            let bitmap = try XCTUnwrap(NSBitmapImageRep(data: Data(contentsOf: url)))
            XCTAssertEqual(bitmap.pixelsWide, 64, identity)
            XCTAssertEqual(bitmap.pixelsHigh, 64, identity)
            var minimumAlpha = CGFloat(1)
            var maximumAlpha = CGFloat(0)
            var visibleColors = Set<String>()
            for y in 0..<bitmap.pixelsHigh {
                for x in 0..<bitmap.pixelsWide {
                    guard let color = bitmap.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB) else { continue }
                    minimumAlpha = min(minimumAlpha, color.alphaComponent)
                    maximumAlpha = max(maximumAlpha, color.alphaComponent)
                    if color.alphaComponent > 0.1 {
                        visibleColors.insert(String(format: "%.2f:%.2f:%.2f", color.redComponent, color.greenComponent, color.blueComponent))
                    }
                }
            }
            XCTAssertLessThan(minimumAlpha, 0.05, identity)
            XCTAssertGreaterThan(maximumAlpha, 0.95, identity)
            XCTAssertGreaterThanOrEqual(visibleColors.count, 1, identity)
        }
    }

    func testDefaultPopoverWiresExpandedPersistedUsageSection() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let content = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/UI/ContentStackAndRows.swift"),
            encoding: .utf8
        )
        let config = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/Data/Config.swift"),
            encoding: .utf8
        )
        let delegate = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/App/AppDelegate.swift"),
            encoding: .utf8
        )
        let popover = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/App/PopoverController.swift"),
            encoding: .utf8
        )

        XCTAssertTrue(content.contains("place(UsagePeekRow("))
        XCTAssertTrue(content.contains("collapsed: config.usagePeekCollapsed"))
        XCTAssertTrue(content.contains("setAccessibilityRole(.button)"))
        XCTAssertTrue(content.contains("onOpen: { [weak self] in self?.onAction?(.openUsage) }"))
        XCTAssertTrue(content.contains("override func accessibilityPerformPress() -> Bool"))
        XCTAssertTrue(config.contains("var usagePeekCollapsed: Bool = false"))
        XCTAssertTrue(config.contains("decodeIfPresent(Bool.self, forKey: .usagePeekCollapsed)"))
        XCTAssertTrue(delegate.contains("popover.updateUsage(state)"))
        XCTAssertTrue(popover.contains("case .setUsagePeekCollapsed(let collapsed):"))
        XCTAssertTrue(popover.contains("next.save()"))
    }
    func testNativeUsageKeepsFullCapabilitiesBehindDefaultClosedDisclosure() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/Shared/Sources/UsageDashboardContent.swift"),
            encoding: .utf8
        )
        XCTAssertTrue(source.contains("@State private var showingDetails = false"))
        XCTAssertTrue(source.contains("DisclosureGroup(\"Details, provenance & history\""))
        for capability in [
            "quotaSection", "historySection", "UsageModelBreakdownSection",
            "UsageProjectBreakdownSection", "costSection", "operationalDetails", "sourceNotes",
        ] {
            XCTAssertTrue(source.contains(capability), capability)
        }
    }

    func testNativeUsageTrendOverviewRendersOutsideClosedProviderDisclosures() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/Shared/Sources/UsageDashboardContent.swift"),
            encoding: .utf8
        )

        let providerCardStart = try XCTUnwrap(source.range(of: "private struct UsageProviderCard"))
        let alwaysVisibleDashboard = String(source[..<providerCardStart.lowerBound])
        XCTAssertTrue(alwaysVisibleDashboard.contains("private var dailyTrends: [UsageDailyCostTrendProjection]"))
        XCTAssertTrue(alwaysVisibleDashboard.contains("UsageHistoryPresentation.sources(for: card.provider)"))
        XCTAssertTrue(alwaysVisibleDashboard.contains("UsageDailyTrendOverview("))
        XCTAssertTrue(alwaysVisibleDashboard.contains("projections: dailyTrends"))
        XCTAssertTrue(alwaysVisibleDashboard.contains("title: \"Daily estimated cost\""))
        XCTAssertTrue(alwaysVisibleDashboard.contains("Daily cost history unavailable."))

        let disclosureStart = try XCTUnwrap(source.range(of: "DisclosureGroup(\"Details, provenance & history\""))
        let overviewCall = try XCTUnwrap(source.range(of: "UsageDailyTrendOverview("))
        XCTAssertLessThan(
            source.distance(from: source.startIndex, to: overviewCall.lowerBound),
            source.distance(from: source.startIndex, to: disclosureStart.lowerBound),
            "The daily graph must render in the default dashboard flow, not only inside the closed disclosure."
        )
    }

    func testNativeCockpitUsesPersistentSingleRowUsageStripAboveBoard() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let shared = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/Shared/Sources/UsageDashboardContent.swift"),
            encoding: .utf8
        )
        let cockpit = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/macOS/Sources/MacCockpitView.swift"),
            encoding: .utf8
        )
        let telemetry = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/Shared/Sources/SystemTelemetryView.swift"),
            encoding: .utf8
        )

        XCTAssertTrue(shared.contains("struct UsageCompactBoardStrip: View"))
        XCTAssertTrue(shared.contains("@State private var expanded = false"))
        XCTAssertFalse(shared.contains("@AppStorage(\"coord.cockpit.usage-strip-expanded\")"))
        XCTAssertTrue(shared.contains("let systemTelemetry: SystemTelemetrySnapshot?"))
        XCTAssertTrue(shared.contains("snapshot: systemTelemetry, showDisk: true, embedded: true"))
        XCTAssertTrue(shared.contains("expanded: true"))
        XCTAssertTrue(shared.contains("Collapse usage and system stats"))
        XCTAssertTrue(shared.contains(".onAppear {"))
        XCTAssertTrue(shared.contains("onExpandedChange?(false)"))
        XCTAssertTrue(shared.contains("Text(\"TOTAL TOKEN COST\")"))
        XCTAssertTrue(shared.contains("HStack(alignment: .top, spacing: 12)"))
        XCTAssertFalse(shared.contains("GridItem(.adaptive(minimum: 230)"))
        XCTAssertTrue(telemetry.contains("var showDisk = true"))
        XCTAssertTrue(telemetry.contains("var embedded = false"))
        XCTAssertTrue(telemetry.contains("return values.filter { showDisk || $0.0 != \"DISK\" }"))
        XCTAssertTrue(telemetry.contains(".frame(height: embedded ? 38 : 30)"))
        XCTAssertTrue(telemetry.contains("private var expandedCockpitStrip"))
        XCTAssertTrue(telemetry.contains(".frame(maxWidth: .infinity, minHeight: 68, maxHeight: 68)"))
        XCTAssertFalse(telemetry.contains("tinyUtilizationGraph"))
        XCTAssertFalse(telemetry.contains("@State private var history: [SystemTelemetrySnapshot] = []"))
        XCTAssertTrue(telemetry.contains("SystemTelemetrySnapshot.MemoryRingComposition.make(snapshot?.memory)"))
        XCTAssertTrue(telemetry.contains("case .compressed: return .pink"))
        XCTAssertTrue(shared.contains(".frame(height: 38)"))
        XCTAssertTrue(shared.contains("windows.append((\"S\", session))"))
        XCTAssertTrue(shared.contains("windows.append((\"W\", weekly))"))
        XCTAssertTrue(shared.contains("windows.append((\"F\", fable))"))
        XCTAssertFalse(shared.contains("selectedQuota("))
        XCTAssertTrue(shared.contains("Text(\"Cost\")"))
        XCTAssertTrue(shared.contains(".font(.system(size: 8.5, weight: .regular).monospacedDigit())"))
        XCTAssertTrue(shared.contains("var barPalette: UsageBarPalette = .colored"))
        XCTAssertTrue(shared.contains(".background(.ultraThinMaterial"))
        XCTAssertFalse(String(shared[..<shared.range(of: "struct UsageDashboardContent")!.lowerBound]).contains("ScrollView"))

        let strip = try XCTUnwrap(cockpit.range(of: "UsageCompactBoardStrip"))
        let board = try XCTUnwrap(cockpit.range(of: "if let snapshot = model.snapshot", range: strip.upperBound..<cockpit.endIndex))
        XCTAssertLessThan(strip.lowerBound, board.lowerBound)
        XCTAssertTrue(cockpit.contains("systemTelemetry: model.systemTelemetry"))
        XCTAssertTrue(cockpit.contains("coord.cockpit.system-telemetry-visible"))
        XCTAssertFalse(cockpit.contains("SystemTelemetryStrip(snapshot: model.systemTelemetry)"))
        XCTAssertEqual(cockpit.components(separatedBy: "UsageCompactBoardStrip(").count - 1, 1)

        let coordRAM = try XCTUnwrap(telemetry.range(of: #"("RAM", "memorychip""#))
        let coordGPU = try XCTUnwrap(telemetry.range(of: #"("GPU", "display""#))
        let coordCPU = try XCTUnwrap(telemetry.range(of: #"("CPU", "cpu""#))
        let coordDisk = try XCTUnwrap(telemetry.range(of: #"("DISK", "internaldrive""#))
        XCTAssertLessThan(coordRAM.lowerBound, coordGPU.lowerBound)
        XCTAssertLessThan(coordGPU.lowerBound, coordCPU.lowerBound)
        XCTAssertLessThan(coordCPU.lowerBound, coordDisk.lowerBound)

    }

    func testUsageEndpointIsLoopbackOnlyAndUsesCanonicalBoardRoute() throws {
        let client = SnapshotClient()
        let loopback = try XCTUnwrap(URL(string: EndpointTestFixtures.loopbackControl))
        XCTAssertEqual(
            try client.usageURL(baseURL: loopback).absoluteString,
            EndpointTestFixtures.loopbackControlUsageDashboard
        )
        for value in ["https://example.test", EndpointTestFixtures.lanOrigin] {
            XCTAssertThrowsError(try client.usageURL(baseURL: XCTUnwrap(URL(string: value)))) { error in
                XCTAssertEqual(error as? SnapshotError, .usageRequiresLoopback)
            }
        }
    }

    @MainActor
    func testTransientStaleRefreshingRetriesUntilFresh() async throws {
        let stale = try decoder().decode(
            UsageIntelligenceSnapshot.self,
            from: Data(#"{"schema":"coordharness.usage-intelligence.v1","refresh":{"state":"stale_refreshing"},"providers":{"claude":{"account":{"authenticated":true},"history":{"today_total_tokens":11}}}}"#.utf8)
        )
        let fresh = try decoder().decode(
            UsageIntelligenceSnapshot.self,
            from: Data(#"{"schema":"coordharness.usage-intelligence.v1","refresh":{"state":"fresh"},"providers":{"claude":{"account":{"authenticated":true},"history":{"today_total_tokens":22}}}}"#.utf8)
        )
        let sequence = InstalledUsageSnapshotSequence([stale, stale, fresh])

        let snapshot = try await UsageTransientRefreshRetry.resolve(
            warmingRetryDelay: 0,
            staleRefreshingRetryDelay: 0,
            retryLimit: 3,
            load: { try await sequence.next() },
            sleep: { _ in }
        )

        let fetchCount = await sequence.count()
        let state = UsageDashboardState.success(snapshot, at: Date())
        XCTAssertEqual(fetchCount, 3)
        XCTAssertEqual(state.snapshot?.refresh?.state, "fresh")
        XCTAssertEqual(state.snapshot?.providers["claude"]?.history?.todayTotalTokens, 22)
        XCTAssertFalse(state.stale)
        XCTAssertFalse(state.refreshing)
    }

    @MainActor
    func testTransientStaleRefreshingRetryBudgetIsBounded() async throws {
        let stale = try decoder().decode(
            UsageIntelligenceSnapshot.self,
            from: Data(#"{"schema":"coordharness.usage-intelligence.v1","refresh":{"state":"stale_refreshing"},"providers":{"codex":{"account":{"authenticated":true},"history":{"today_total_tokens":7}}}}"#.utf8)
        )
        let sequence = InstalledUsageSnapshotSequence([stale])

        let snapshot = try await UsageTransientRefreshRetry.resolve(
            warmingRetryDelay: 0,
            staleRefreshingRetryDelay: 0,
            retryLimit: 2,
            load: { try await sequence.next() },
            sleep: { _ in }
        )

        let fetchCount = await sequence.count()
        let state = UsageDashboardState.success(snapshot, at: Date())
        XCTAssertEqual(fetchCount, 3, "One initial fetch plus exactly two bounded retries")
        XCTAssertEqual(state.snapshot?.refresh?.state, "stale_refreshing")
        XCTAssertTrue(state.stale)
        XCTAssertTrue(state.refreshing)
    }

    func testUsageOpenDetailsAndFooterForceRefreshWhileWorkRefreshRemainsWired() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let installedUsage = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/Usage/InstalledUsageDashboard.swift"),
            encoding: .utf8
        )
        let popover = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/App/PopoverController.swift"),
            encoding: .utf8
        )
        let cockpit = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/Cockpit/UI/CockpitRootView.swift"),
            encoding: .utf8
        )
        let delegate = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/App/AppDelegate.swift"),
            encoding: .utf8
        )
        let showStart = try XCTUnwrap(popover.range(of: "func show(relativeTo button: NSStatusBarButton)"))
        let showEnd = try XCTUnwrap(popover.range(of: "func close()", range: showStart.upperBound..<popover.endIndex))
        let showRoute = String(popover[showStart.lowerBound..<showEnd.lowerBound])
        let performStart = try XCTUnwrap(popover.range(of: "private func perform(_ action: PanelAction)"))
        let performEnd = try XCTUnwrap(popover.range(of: "private func toggleDetachedPanel()", range: performStart.upperBound..<popover.endIndex))
        let performRoute = String(popover[performStart.lowerBound..<performEnd.lowerBound])
        let detailsStart = try XCTUnwrap(popover.range(of: "private func showUsage()"))
        let detailsEnd = try XCTUnwrap(popover.range(of: "private func exitUsage()", range: detailsStart.upperBound..<popover.endIndex))
        let detailsRoute = String(popover[detailsStart.lowerBound..<detailsEnd.lowerBound])
        let usageSurfaceStart = try XCTUnwrap(cockpit.range(of: "if path == Surface.usage.rawValue"))
        let usageSurfaceEnd = try XCTUnwrap(cockpit.range(of: "if path == Surface.cockpit.rawValue", range: usageSurfaceStart.upperBound..<cockpit.endIndex))
        let usageSurface = String(cockpit[usageSurfaceStart.lowerBound..<usageSurfaceEnd.lowerBound])

        XCTAssertTrue(installedUsage.contains("func refresh(force: Bool = false) async"))
        XCTAssertTrue(installedUsage.contains("await store.refresh(force: true)"))
        XCTAssertTrue(showRoute.contains("forceUsageRefresh()"))
        XCTAssertTrue(detailsRoute.contains("forceUsageRefresh()"))
        XCTAssertTrue(usageSurface.contains("await self.usageStore.refresh(force: true)"))
        XCTAssertTrue(performRoute.contains("case .refresh:                  forceUsageRefresh()"))
        XCTAssertTrue(performRoute.contains("onWantsRefresh?()"), "Footer refresh must continue updating work state")
        XCTAssertTrue(delegate.contains("popover.onWantsRefresh = { [weak self] in self?.refresh() }"))
    }

    func testInstalledCockpitRootMountsBoardSafeUsageAndStatsStrip() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        func source(_ path: String) throws -> String {
            try String(contentsOf: projectRoot.appendingPathComponent(path), encoding: .utf8)
        }

        let root = try source("apps/menubar/Sources/Cockpit/UI/CockpitRootView.swift")
        let controller = try source("apps/menubar/Sources/Cockpit/UI/CockpitWindowController.swift")
        let delegate = try source("apps/menubar/Sources/App/AppDelegate.swift")
        let usage = try source("apps/Shared/Sources/UsageDashboardContent.swift")
        let telemetry = try source("apps/Shared/Sources/SystemTelemetryView.swift")

        XCTAssertTrue(root.contains("NSHostingView<InstalledCockpitUsageStrip>"))
        XCTAssertTrue(root.contains("UsageCompactBoardStrip("))
        XCTAssertTrue(root.contains("@ObservedObject var usageStore: InstalledUsageStore"))
        XCTAssertTrue(root.contains("@ObservedObject var telemetryStore: SystemTelemetryStore"))
        XCTAssertTrue(root.contains("private var usageStripExpanded = false"))
        XCTAssertTrue(root.contains("let usageStripHeight: CGFloat = usageStripExpanded ? 194 : 38"))
        XCTAssertTrue(root.contains("let boardY = usageStripY + usageStripHeight + expandedGap"))
        XCTAssertTrue(root.contains("y: boardY"))
        XCTAssertTrue(root.contains("height: max(120, bounds.height - boardY - 16)"))
        XCTAssertTrue(root.contains("usageStripView.isHidden = !cockpitVisible"))
        XCTAssertTrue(root.contains("func prepareForPresentation()"))
        XCTAssertTrue(controller.contains("rootView.prepareForPresentation()"))
        XCTAssertTrue(controller.contains("telemetryStore: telemetryStore"))
        XCTAssertTrue(delegate.contains("telemetryStore: telemetryStore"))

        XCTAssertTrue(usage.contains("Text(\"TOTAL TOKEN COST\")"))
        XCTAssertTrue(usage.contains("HStack(alignment: .top, spacing: 12)"))
        XCTAssertTrue(usage.contains("onExpandedChange?(next)"))
        XCTAssertTrue(usage.contains("showDisk: true"))
        XCTAssertTrue(telemetry.contains(".frame(maxWidth: .infinity, minHeight: 68, maxHeight: 68)"))
        XCTAssertFalse(telemetry.contains("tinyUtilizationGraph"))

        let ram = try XCTUnwrap(telemetry.range(of: #"("RAM", "memorychip""#))
        let gpu = try XCTUnwrap(telemetry.range(of: #"("GPU", "display""#))
        let cpu = try XCTUnwrap(telemetry.range(of: #"("CPU", "cpu""#))
        let disk = try XCTUnwrap(telemetry.range(of: #"("DISK", "internaldrive""#))
        XCTAssertLessThan(ram.lowerBound, gpu.lowerBound)
        XCTAssertLessThan(gpu.lowerBound, cpu.lowerBound)
        XCTAssertLessThan(cpu.lowerBound, disk.lowerBound)
    }

    func testInstalledUsageRouteMaintainsDensePublicContract() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        func source(_ path: String) throws -> String {
            try String(contentsOf: projectRoot.appendingPathComponent(path), encoding: .utf8)
        }
        func assertOrdered(_ labels: [String], in source: String, file: StaticString = #filePath, line: UInt = #line) {
            var cursor = source.startIndex
            for label in labels {
                guard let range = source.range(of: label, range: cursor..<source.endIndex) else {
                    XCTFail("Missing visible label \(label)", file: file, line: line)
                    return
                }
                cursor = range.upperBound
            }
        }

        let coordContent = try source("apps/Shared/Sources/UsageDashboardContent.swift")
        let coordRoute = try source("apps/menubar/Sources/App/PopoverController.swift")
        let installed = try source("apps/menubar/Sources/Usage/InstalledUsageDashboard.swift")
        // The installed route—not merely an incidental shared card—must use the dense
        // composition: total strip, ordered providers, quotas, daily bars, and a
        // persistent action rail.
        let requiredSections = [
            "UsageDenseTotalCostStrip",
            "UsageDenseProviderSection",
            "UsageDenseQuotaRow",
            "UsageDenseDailyCostChart",
            "UsageDashboardFooter",
            "onOpenSettings",
        ]
        for token in requiredSections {
            XCTAssertTrue(coordContent.contains(token), "COORD Usage route lost \(token)")
        }

        let requiredGeometry = [
            "compactChartHeight: CGFloat = 74",
            "providerHorizontalPadding: CGFloat = 18",
            "providerVerticalPadding: CGFloat = 14",
            "providerCornerRadius: CGFloat = 12",
            "providerBorderOpacity: CGFloat = 0.23",
            "providerFactsSpacing: CGFloat = 22",
            "providerChartWidth: CGFloat = 300",
            "Color(red: 0.95, green: 0.47, blue: 0.24)",
            "Color(red: 0.66, green: 0.42, blue: 1.00)",
            ".font(.system(size: 19, weight: .bold, design: .rounded))",
        ]
        for token in requiredGeometry {
            XCTAssertTrue(coordContent.contains(token), "COORD dense geometry drifted from \(token)")
        }

        let visibleLabelOrder = ["Total Tokens Costs", "Today", "Retained cost", "Tokens", "Daily cost"]
        let denseRouteStart = try XCTUnwrap(coordContent.range(of: "private struct UsageDenseRoute: View"))
        let denseRouteEnd = try XCTUnwrap(coordContent.range(of: "private struct UsageDailyTrendOverview", range: denseRouteStart.upperBound..<coordContent.endIndex))
        let denseRouteSource = String(coordContent[denseRouteStart.lowerBound..<denseRouteEnd.lowerBound])
        assertOrdered(visibleLabelOrder, in: denseRouteSource)
        XCTAssertTrue(coordContent.contains("visibleLabelOrder = [\"Total Tokens Costs\", \"Claude\", \"Codex\""))
        XCTAssertTrue(coordContent.contains("case \"claude\": 0"))
        XCTAssertTrue(coordContent.contains("case \"codex\": 1"))

        XCTAssertTrue(installed.contains("usesDenseRoute: true"))
        XCTAssertTrue(installed.contains("onRefresh: { Task { await store.refresh(force: true) } }"))
        XCTAssertTrue(installed.contains("UsageAccountSettingsView("))
        XCTAssertTrue(coordRoute.contains("max(42, availablePopoverHeight())"))
        XCTAssertTrue(coordContent.contains("Refresh provider usage"))
        XCTAssertTrue(coordContent.contains("Provider settings"))
    }

    func testUsageWindowUsesDollarCostLabelsAndAContainedTallerCodexPlot() throws {
        let projectRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let content = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/Shared/Sources/UsageDashboardContent.swift"),
            encoding: .utf8
        )
        let popover = try String(
            contentsOf: projectRoot.appendingPathComponent("apps/menubar/Sources/App/PopoverController.swift"),
            encoding: .utf8
        )
        let denseStart = try XCTUnwrap(content.range(of: "private struct UsageDenseRoute: View"))
        let denseEnd = try XCTUnwrap(content.range(of: "private struct UsageDailyTrendOverview", range: denseStart.upperBound..<content.endIndex))
        let dense = String(content[denseStart.lowerBound..<denseEnd.lowerBound])

        XCTAssertTrue(popover.contains("let usageWindowWidth: CGFloat = 460"))
        XCTAssertTrue(popover.contains("max(usageWindowWidth, detachedSize?.width ?? usageWindowWidth)"))
        XCTAssertTrue(content.contains("private enum UsageDashboardCostFormat"))
        XCTAssertTrue(content.contains("replacingOccurrences(of: \"USD \", with: \"$\")"))
        XCTAssertTrue(dense.contains("UsageDashboardCostFormat.display(totalEstimatedCostNanos)"))
        XCTAssertTrue(dense.contains("UsageDashboardCostFormat.display(latest?.nanos"))

        XCTAssertTrue(content.contains("claudeChartPlotHeight: CGFloat = 82"))
        XCTAssertTrue(content.contains("codexChartPlotHeight: CGFloat = 136"))
        XCTAssertTrue(dense.contains("case \"claude\": return UsageDenseRouteLayout.claudeChartPlotHeight"))
        XCTAssertTrue(dense.contains("case \"codex\": return UsageDenseRouteLayout.codexChartPlotHeight"))
        XCTAssertTrue(dense.contains("plotHeight: effectiveChartPlotHeight"))
        XCTAssertTrue(dense.contains("chartPanelHeight"))
        XCTAssertTrue(dense.contains("chart.frame(width: UsageDenseRouteLayout.providerChartWidth, height: chartPanelHeight)"))
        XCTAssertTrue(dense.contains(".frame(height: plotHeight, alignment: .bottom)"))
        XCTAssertTrue(dense.contains("ScrollView"), "Taller Codex content must remain scrollable rather than clip.")
    }
    private func fixture(named name: String = "usage-dashboard-v1") throws -> UsageIntelligenceSnapshot {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: name, withExtension: "json"))
        return try decoder().decode(UsageIntelligenceSnapshot.self, from: Data(contentsOf: url))
    }

    private func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }
}
private actor InstalledUsageSnapshotSequence {
    private let snapshots: [UsageIntelligenceSnapshot]
    private var index = 0
    private var fetches = 0

    init(_ snapshots: [UsageIntelligenceSnapshot]) {
        self.snapshots = snapshots
    }

    func next() async throws -> UsageIntelligenceSnapshot {
        guard let last = snapshots.last else {
            throw UsageIntelligenceSnapshotError.emptyOrErrorEnvelope
        }
        let snapshot = snapshots[min(index, snapshots.count - 1)]
        index += 1
        fetches += 1
        return index <= snapshots.count ? snapshot : last
    }

    func count() -> Int {
        fetches
    }
}
