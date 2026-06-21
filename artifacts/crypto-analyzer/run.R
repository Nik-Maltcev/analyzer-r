lib_user <- Sys.getenv("R_LIBS_USER", unset = "/home/runner/workspace/.config/R")
.libPaths(c(lib_user, .libPaths()))

port <- as.integer(Sys.getenv("PORT", unset = "3000"))

shiny::runApp(
  appDir = dirname(sys.frame(1)$ofile %||% "/home/runner/workspace/artifacts/crypto-analyzer"),
  host = "0.0.0.0",
  port = port,
  launch.browser = FALSE
)
