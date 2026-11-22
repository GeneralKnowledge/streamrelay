import Foundation

/// Exponential backoff with full jitter (protocol v1 client rules): the n-th
/// consecutive failure waits a random time in [0, min(cap, base * 2^n)];
/// a success resets it. A server's Retry-After wins over the computed wait.
public struct Backoff: Sendable {
    public var base: Double
    public var cap: Double
    public private(set) var failures = 0

    public init(base: Double = 1, cap: Double = 30) {
        self.base = base
        self.cap = cap
    }

    /// The ceiling for the next wait.
    public var ceiling: Double { min(cap, base * pow(2, Double(failures))) }

    /// Records a failure and returns how long to wait before the next attempt.
    public mutating func fail(retryAfter: Double? = nil, random: Double = Double.random(in: 0...1)) -> Double {
        let wait = retryAfter.map { min($0, 300) } ?? ceiling * random
        failures = min(failures + 1, 16)
        return wait
    }

    public mutating func succeed() {
        failures = 0
    }
}
