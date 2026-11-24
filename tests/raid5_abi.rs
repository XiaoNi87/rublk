#![cfg(feature = "raid5")]
#[test]
fn bpf_sqe_nonzero_operation_and_descriptor_layout() {
    for op in [0u8, 1, 15, 255] {
        let sqe = io_uring::opcode::Bpf::new(0x12340000usize as *const u8, 1)
            .operation(op)
            .build();
        // SAFETY: initialized repr-transparent Entry contains a zeroed 64-byte SQE.
        let bytes: [u8; 64] = unsafe { std::mem::transmute(sqe) };
        assert_eq!(bytes[0], 65);
        assert_eq!(
            u64::from_ne_bytes(bytes[16..24].try_into().unwrap()),
            0x12340000
        );
        assert_eq!(u32::from_ne_bytes(bytes[24..28].try_into().unwrap()), 1);
        assert_eq!(
            u32::from_ne_bytes(bytes[28..32].try_into().unwrap()),
            (op as u32) << 24
        );
    }
    assert_eq!(std::mem::size_of::<io_uring::bpf::BufferDescriptor>(), 16);
}
