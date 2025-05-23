$dirs = Get-ChildItem -Recurse -Directory | Sort-Object FullName -Descending

foreach ($dir in $dirs) {
    $originalPath = $dir.FullName
    $relativePath = $originalPath.Substring((Get-Location).Path.Length + 1)
    $lowerPath = $relativePath.ToLower()

    if ($relativePath -ne $lowerPath) {
        $tmpPath = "$lowerPath-tmp"

        git mv "$relativePath" "$tmpPath"
        git mv "$tmpPath" "$lowerPath"
    }
}

git commit -m "Rename all directories to lowercase"
git push