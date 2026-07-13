//
//  DeviceID.swift
//  BaseballStats
//
//  Stable per-install identifier sent as `X-Device-Id` on /ask requests.
//  The backend rate-limits questions per device (30/day) using this value.
//  `identifierForVendor` is stable for the life of the install and resets
//  only if every app from this vendor is removed — good enough for a soft
//  per-device quota. Falls back to a fixed sentinel on the rare occasion
//  the system returns nil (it can briefly after a device restart before
//  first unlock); the backend treats the sentinel like any other id.
//

import UIKit

enum DeviceID {
    static let current: String =
        UIDevice.current.identifierForVendor?.uuidString ?? "unknown-device"
}
