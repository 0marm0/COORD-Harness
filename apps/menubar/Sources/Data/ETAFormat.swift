import Foundation


enum ETAFormat {


    static func fmtETA(_ secs: Double) -> String? {
        guard secs > 0, secs < 7 * 24 * 3600 else { return nil }
        let mins = secs / 60
        if mins >= 60 {
            let hours = (mins / 30).rounded() / 2
            return hours == hours.rounded() ? "\(Int(hours))h" : "\(Int(hours)).5h"
        }
        return "\(max(1, Int(mins.rounded())))m"
    }


    static func spaced(_ s: String) -> String {
        var out = ""
        let chars = Array(s)
        for (i, c) in chars.enumerated() {
            if c.isLetter, i > 0, chars[i - 1].isNumber { out.append(" ") }
            out.append(c)
        }
        return out
    }


    static func parse(_ s: String) -> Double? {
        let t = s.trimmingCharacters(in: .whitespaces)
        if t.isEmpty || t == "—" || t == "~" { return nil }
        var secs = 0.0, any = false
        for tok in t.split(separator: " ") {
            let str = String(tok)
            if str.hasSuffix("h"), let v = Double(str.dropLast()) { secs += v * 3600; any = true }
            else if str.hasSuffix("m"), let v = Double(str.dropLast()) { secs += v * 60; any = true }
            else if str.hasSuffix("s"), let v = Double(str.dropLast()) { secs += v; any = true }
        }
        return any ? secs : nil
    }
}
