# Distribute to Google Play Console

A GitHub Action that uploads an Android App Bundle (`.aab`) or APK to a
[Google Play Console](https://play.google.com/console) track (internal, closed,
open testing or production), with release notes, staged rollouts and
deobfuscation files.

It is the Play counterpart of
[`distribute_to_firebase`](https://github.com/crianpiro/distribute_to_firebase)
and [`upload_build_to_testflight`](https://github.com/crianpiro/upload_build_to_testflight),
and is built to slot into
[`flutter_release_pipeline`](https://github.com/crianpiro/flutter_release_pipeline)
(see [Using it from flutter_release_pipeline](#using-it-from-flutter_release_pipeline)).

What it adds beyond a bare API call:

- **The package name is read from the artifact.** The AAB or APK says which
  app it belongs to, just as the IPA does for the TestFlight action. A flavored
  pipeline needs no per-flavor package secret. If you set `package-name`
  anyway, a mismatch fails before anything is uploaded, so you can't ship one
  flavor's build to another flavor's listing.
- **All or nothing.** Everything goes through one Play *edit*. If anything
  fails before the commit, the edit is discarded, so Play never ends up with
  an uploaded bundle but no release, or a release with no notes.
- **Inputs are checked before Google is contacted.** A bad status, rollout
  fraction or credentials file fails in seconds and leaves no edit behind.
- **Errors say what to do.** The common Play refusals (a draft app, a reused
  version code, a service account that was never invited, changes that must
  be sent for review by hand) come with the fix, not just the API message.
- **Long notes are shortened, not fatal.** Play accepts 500 characters of
  "What's new" per language. Longer notes are cut to fit with a warning,
  because a pull request description should not fail a good build.
- **Nothing to install.** It needs `python3` and `openssl`, which every hosted
  runner image ships, and nothing from pip. That also makes it work on a bare
  self-hosted runner. The service account key is written only under
  `RUNNER_TEMP` and is deleted as soon as the token is signed.
- **No shell injection.** Every input reaches the script through the
  environment, never by being pasted into a command, so release notes
  containing backticks or `$( )` are just text.

## Usage

```yaml
- name: Distribute to Google Play
  uses: crianpiro/distribute_to_play_console@v1
  with:
    artifact: build/app/outputs/bundle/release/app-release.aab
    credentials: ${{ secrets.PLAY_SERVICE_ACCOUNT }}
    track: internal
    release-notes: ${{ github.event.head_commit.message }}
```

### Staged rollout to production

```yaml
- uses: crianpiro/distribute_to_play_console@v1
  with:
    artifact: build/app/outputs/bundle/release/app-release.aab
    credentials: ${{ secrets.PLAY_SERVICE_ACCOUNT }}
    track: production
    status: inProgress
    user-fraction: "0.1"
    release-notes-file: distribution/whatsnew-en-US.txt
    mapping-file: build/app/outputs/mapping/release/mapping.txt
```

### Using the outputs

```yaml
- uses: crianpiro/distribute_to_play_console@v1
  id: play
  with:
    artifact: app-release.aab
    credentials: ${{ secrets.PLAY_SERVICE_ACCOUNT }}

- run: echo "Play assigned version code ${{ steps.play.outputs.version-code }}"
```

## Inputs

| Input | Default | |
|---|---|---|
| `artifact` | **required** | Path to the `.aab`, or the `.apk` for an app that still ships APKs. |
| `credentials` | one of | Contents of the service account's JSON key. Pass it through a secret. |
| `credentials-file` | one of | Path to the service account's JSON key file. |
| `package-name` | read from the artifact | The app's `applicationId`. When set, it must match the artifact. |
| `track` | `internal` | `internal`, `alpha` (closed testing), `beta` (open testing), `production`, or the name of a custom closed testing track. |
| `status` | `completed` | `completed` (full rollout), `inProgress` (staged rollout, needs `user-fraction`), `draft`, or `halted`. An app that has never been published accepts only `draft`. |
| `user-fraction` | empty | Share of users in a staged rollout: greater than 0 and less than 1. |
| `release-name` | the version name | The release's name in the Play Console (up to 50 characters). |
| `release-notes` | empty | "What's new in this version". Shortened to 500 characters, with a warning, if longer. |
| `release-notes-file` | empty | Path to a file holding the release notes. Use this or `release-notes`, not both. |
| `release-notes-language` | `en-US` | Language of the release notes, as Play lists it for the app. |
| `in-app-update-priority` | empty | `0`–`5`, for the [in-app updates](https://developer.android.com/guide/playcore/in-app-updates) API. |
| `mapping-file` | empty | R8/ProGuard `mapping.txt`, for readable crash stack traces. |
| `native-debug-symbols` | empty | Zip of native debug symbols. An AAB built with `ndk.debugSymbolLevel` already carries them, so this is mainly for APKs. |
| `changes-not-sent-for-review` | `false` | Commit without sending the changes for review. Needed when Play answers "Changes cannot be sent for review automatically". |

Give exactly one of `credentials` and `credentials-file`.

## Outputs

| Output | |
|---|---|
| `package-name` | The package the build was published to. |
| `version-code` | The version code Play read from the upload. |
| `track` | The track it was released on. |
| `status` | The status it was released with. |

## Getting the credentials

The Play Developer API is reached through a Google Cloud **service account**
that has been invited to your Play developer account.

1. **Create the service account.** In the
   [Google Cloud console](https://console.cloud.google.com/iam-admin/serviceaccounts),
   choose any project (or create one), then go to IAM & Admin → Service accounts
   → Create. It needs no Cloud IAM role.
2. **Enable the API** in that project:
   [Google Play Android Developer API](https://console.cloud.google.com/apis/library/androidpublisher.googleapis.com).
3. **Create a key.** On the service account, go to Keys → Add key → JSON
   ([Create and delete keys](https://cloud.google.com/iam/docs/keys-create-delete)).
4. **Invite it to Play.** In the Play Console, go to Users and permissions →
   Invite new users. Enter the service account's email
   (`…@….iam.gserviceaccount.com`) and, under App permissions, add the app
   with at least *Release to testing tracks* (and *Release to production…*
   if you will publish there). A new invitation can take a while to start
   working.
5. **Store the key** as a secret, then delete the local file:

   ```bash
   gh secret set PLAY_SERVICE_ACCOUNT < key.json
   ```

Two things the API cannot do for you:

- **Create the app.** The app must exist in the Play Console, and its very
  first build must be uploaded there by hand, before the API can see it.
- **Release an unpublished app with any status but `draft`.** Until the app's
  first release has been rolled out from the Console, use `status: draft`.

## Using it from flutter_release_pipeline

[`flutter_release_pipeline`](https://github.com/crianpiro/flutter_release_pipeline)
ships Android builds to Firebase with `distribute-android`. Play can be wired
in the same way, as a third distribution job that follows the same
conventions:

- **The artifact is an AAB.** Play has accepted only App Bundles for new apps
  since August 2021. The Android build job adds
  `flutter build appbundle --release ${FLAVOR:+--flavor "$FLAVOR"} --target "$TARGET"`
  next to the APK, which lands in
  `build/app/outputs/bundle/${FLAVOR:+${FLAVOR}}Release/app-${FLAVOR:+$FLAVOR-}release.aab`.
  It uploads it as `<artifact-name>-<version>+<build>.aab`, alongside the APK.
- **The version code is the build number.** The pipeline bumps `+N` in
  `pubspec.yaml` on every release, and Play, like App Store Connect, refuses a
  version code it has seen before. The two rules agree, so nothing more is
  needed.
- **Flavors need nothing extra.** The package name comes from the bundle, so a
  flavored caller passes the service account like any other secret, with the
  usual suffix fallback:
  `PLAY_SERVICE_ACCOUNT: ${{ secrets[format('PLAY_SERVICE_ACCOUNT_{0}', needs.flavor.outputs.suffix)] || secrets.PLAY_SERVICE_ACCOUNT }}`.
- **Release notes pass through unchanged.** They are the `release_notes`
  output the other distributions use, and are shortened to Play's limit if
  needed.

The distribution job, written like the Firebase one:

```yaml
  distribute_artifact_to_play_console:
    name: Distribute Android to Google Play
    needs: [resolve, build_android_artifact]
    if: ${{ needs.resolve.outputs.distribute_play == 'true' }}
    runs-on: ${{ (startsWith(inputs.linux-runner, '{') || startsWith(inputs.linux-runner, '[')) && fromJSON(inputs.linux-runner) || inputs.linux-runner }}
    env:
      ANDROID_BUNDLE_NAME: ${{ needs.resolve.outputs.android_bundle_name }}
    steps:
      - name: Download Android bundle
        uses: actions/download-artifact@v4
        with:
          name: ${{ env.ANDROID_BUNDLE_NAME }}
          path: ./artifacts/

      - name: Distribute Android bundle to Google Play
        uses: crianpiro/distribute_to_play_console@v1
        with:
          artifact: ./artifacts/${{ env.ANDROID_BUNDLE_NAME }}
          credentials: ${{ secrets.PLAY_SERVICE_ACCOUNT }}
          track: ${{ needs.resolve.outputs.play_track }}
          release-name: ${{ needs.resolve.outputs.tag }}
          release-notes: ${{ needs.resolve.outputs.release_notes }}
```

On the pipeline side, the change is: a `distribute-play` keyword (with an
optional `track: <name>` line, defaulting to `internal`) read in `resolve`, the
`distribute_play`, `play_track` and `android_bundle_name` outputs, a
`PLAY_SERVICE_ACCOUNT` secret, and this job added to the release job's `needs`
and to its "at least one distribution landed" rule.

## License

[BSD 3-Clause](./LICENSE) © Cristian Andres Picon Rodriguez
