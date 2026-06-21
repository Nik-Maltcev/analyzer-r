lib_path <- Sys.getenv("R_LIBS_USER")
dir.create(lib_path, recursive = TRUE, showWarnings = FALSE)
.libPaths(c(lib_path, .libPaths()))

pkgs <- c("shiny", "bslib", "plotly", "dplyr", "tidyr", "ggplot2",
          "lubridate", "zoo", "DT", "corrplot", "tibble")

for (pkg in pkgs) {
  if (!requireNamespace(pkg, quietly = TRUE)) {
    cat("Installing", pkg, "...\n")
    install.packages(pkg, repos = "https://cloud.r-project.org", lib = lib_path, quiet = FALSE)
  } else {
    cat(pkg, "already installed\n")
  }
}

cat("Done\n")
