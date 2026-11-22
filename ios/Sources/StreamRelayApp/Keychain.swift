#if os(iOS)
import Foundation
import Security

/// Device tokens, kept in the Keychain on this device only, one per server
/// origin (scheme://host:port), so a token is only ever sent to the server
/// that issued it.
enum Keychain {
    private static let service = "org.forgemobile.streamrelay.token"

    struct Failure: Error, CustomStringConvertible {
        var status: OSStatus
        var description: String { "Keychain error \(status)" }
    }

    private static func query(_ origin: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: origin]
    }

    static func token(for origin: String) -> String? {
        var q = query(origin)
        q[kSecReturnData as String] = true
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }

    /// Stores or replaces the token. Throws if the Keychain didn't take it (the old one, if any, stays).
    static func save(_ token: String, for origin: String) throws {
        let data = Data(token.utf8)
        let update = SecItemUpdate(query(origin) as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if update == errSecSuccess { return }
        guard update == errSecItemNotFound else { throw Failure(status: update) }
        var add = query(origin)
        add[kSecValueData as String] = data
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(add as CFDictionary, nil)
        guard status == errSecSuccess else { throw Failure(status: status) }
    }

    static func delete(for origin: String) {
        SecItemDelete(query(origin) as CFDictionary)
    }
}
#endif
