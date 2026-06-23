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

    func makeUIViewController(context: Context) -> SFSafariViewController {
        let config = SFSafariViewController.Configuration()
        config.entersReaderIfAvailable = false
        let controller = SFSafariViewController(url: url, configuration: config)
        controller.dismissButtonStyle = .done
        return controller
    }

    func updateUIViewController(_ controller: SFSafariViewController, context: Context) {}
}
