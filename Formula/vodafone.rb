# This repository doubles as its own Homebrew tap:
#   brew tap luisgonzaleznf/vodafone-cli https://github.com/luisgonzaleznf/vodafone-cli
#   brew install luisgonzaleznf/vodafone-cli/vodafone
# Releasing a new version (tag, then update `tag:` and `revision:` here): see RELEASING.md.
class Vodafone < Formula
  desc "JSON-first CLI for your own Mi Vodafone (Spain) account"
  homepage "https://github.com/luisgonzaleznf/vodafone-cli"
  # A git URL, not a tarball: the checkout uses your git credentials, so the formula works the same
  # while the repository is private and once it is public.
  url "https://github.com/luisgonzaleznf/vodafone-cli.git",
      tag:      "v0.3.0",
      revision: "58ac7b5205995223c0fd9a23a47f0dcd37819fd8"
  license "MIT"
  head "https://github.com/luisgonzaleznf/vodafone-cli.git", branch: "main"

  depends_on "uv" => :build
  depends_on "python@3.14"

  def install
    # Install exactly the dependency set pinned in uv.lock into libexec. uv uses the locked PyPI
    # wheels, so pydantic-core, rpds-py and cryptography need no Rust toolchain.
    ENV["UV_PROJECT_ENVIRONMENT"] = libexec
    ENV["UV_PYTHON_DOWNLOADS"] = "never"
    system "uv", "sync", "--frozen", "--no-dev", "--no-editable", "--compile-bytecode",
           "--python", formula_opt_bin("python@3.14")/"python3.14"
    bin.install_symlink libexec/"bin/vodafone"
  end

  # Keep the session alive: every refresh also renews the refresh token for about 90 days, so a
  # daily refresh means `vodafone auth login` (with its SMS code) is only needed again if Vodafone
  # ends the session. Opt-in: `brew services start luisgonzaleznf/vodafone-cli/vodafone`.
  service do
    run [opt_bin/"vodafone", "auth", "refresh"]
    run_type :interval
    interval 86400
    log_path var/"log/vodafone-keepalive.log"
    error_log_path var/"log/vodafone-keepalive.log"
  end

  test do
    assert_equal version.to_s, shell_output("#{bin}/vodafone --version").strip unless version.head?
    assert_match "auth", shell_output("#{bin}/vodafone --help")
    # Offline: a missing token file is reported as JSON on stderr with exit code 3.
    output = shell_output("#{bin}/vodafone --tokens #{testpath}/missing.json auth status 2>&1", 3)
    assert_match "auth_expired", output
  end
end
