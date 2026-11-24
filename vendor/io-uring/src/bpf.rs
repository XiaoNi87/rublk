//! Pinned Linux 42abd4d4c487 custom BPF buffer ABI.
#[repr(C)]
#[derive(Debug, Default, Clone, Copy)]
pub struct BufferDescriptor {
    pub kind: u8,
    pub reserved: u8,
    pub buffer_index: u16,
    pub len: u32,
    pub addr: u64,
}
pub const USER: u8 = 0;
pub const FIXED: u8 = 1;
pub const KFIXED: u8 = 3;
const _: () = {
    assert!(std::mem::size_of::<BufferDescriptor>() == 16);
    assert!(std::mem::align_of::<BufferDescriptor>() == 8);
    assert!(std::mem::offset_of!(BufferDescriptor, kind) == 0);
    assert!(std::mem::offset_of!(BufferDescriptor, reserved) == 1);
    assert!(std::mem::offset_of!(BufferDescriptor, buffer_index) == 2);
    assert!(std::mem::offset_of!(BufferDescriptor, len) == 4);
    assert!(std::mem::offset_of!(BufferDescriptor, addr) == 8);
};
