# APECode

APECode compiler and runner compatible with the BAPC 2009 contest language.

## Usage

Compile a submitted program:

```sh
apecc -o Main Main.ape
```

Run a source directly:

```sh
apecode --run Main.ape < input.txt
```

The generated executable embeds the submitted source and imports the installed
`apecode` Python package at runtime.
