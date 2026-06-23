//
//  SafariView.swift
//  BaseballStats
//
//  Thin SwiftUI wrapper around SFSafariViewController so we can open article
//  links (and any external URL) inside the app instead of kicking the user
//  out to Safari. SFSafariViewController adopts the system light/dark
//  appearance automatically.
//

import SafariServices
import SwiftUI

struct SafariView: UIViewControllerRepresentable {
    let url: URL

    // Self-contained: reads the Settings preference directly, so the article
    // always opens with the current "Open articles in Reader Mode" choice no
    // matter where SafariView is presented from.
    @AppStorage("autoReaderMode") private var autoReaderMode = false

    func makeUIViewController(context: Context) -> SFSafariViewController {
        let config = SFSafariViewController.Configuration()
        config.entersReaderIfAvailable = autoReaderMode
        let controller = SFSafariViewController(url: url, configuration: config)
        controller.dismissButtonStyle = .done
        return controller
    }

    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}
