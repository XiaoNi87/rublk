// SPDX-License-Identifier: GPL-2.0-or-later
use super::*;
fn fixtures() -> (tempfile::TempDir, [PathBuf; 3]) {
    let dir = tempfile::tempdir().unwrap();
    let paths = std::array::from_fn(|i| dir.path().join(format!("member{i}")));
    for p in &paths {
        File::create(p).unwrap().set_len(2 * 1048576).unwrap();
    }
    (dir, paths)
}
#[test]
fn duplicate_alias_and_production_file_rejected() {
    let (_dir, paths) = fixtures();
    assert!(Backends::open(paths.clone(), false).is_err());
    let bad = [paths[0].clone(), paths[0].clone(), paths[2].clone()];
    assert!(Backends::open(bad, true).is_err());
}
#[test]
fn ownership_and_metadata_freeze() {
    let (_dir, paths) = fixtures();
    let mut b = Backends::open(paths.clone(), true).unwrap();
    assert!(Backends::open(paths.clone(), true).is_err());
    b.fence().unwrap();
    b.flush(0).unwrap();
    assert_eq!(b.read(0, 0).unwrap(), [0; 4096]);
    b.begin_io();
    assert!(b.fence().is_err());
    assert!(b.write(0, 0, &[0; 4096]).is_err());
    drop(b);
    assert!(Backends::open(paths, true).is_ok());
}
