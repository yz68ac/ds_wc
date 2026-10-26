/* Version-locked libc++ callable adapter. Native coroutine stacks never cross JS. */
typedef struct Function { void **vtable; volatile unsigned int *data; } Function;
extern void *client_alloc(unsigned long);
extern void client_free(void *);
extern int ref_add(volatile unsigned int *, int);
extern void *thread_self(void);
extern int thread_id(void);
extern void *callback_vtable[9];
void destroy_inline(Function *self) {
 if(self->data) { volatile unsigned int *d=self->data;self->data=0;ref_add(d+3,-1); }
}
void destroy_heap(Function *self) { destroy_inline(self);client_free(self); }
Function *clone_heap(Function *self) {
 Function *out=(Function*)client_alloc(sizeof(Function));*out=*self;ref_add(out->data+3,1);return out;
}
void clone_inline(Function *self, Function *out) { *out=*self;ref_add(out->data+3,1); }
typedef struct Input { void *manager;void *core[2];void *future[2];char options[64];volatile unsigned int phase;void *kernel;void *account; } Input;
typedef char input_size_must_be_128[sizeof(Input)==128?1:-1];
typedef char pointer_size_must_be_8[sizeof(void*)==8?1:-1];
extern const unsigned char void_type[];
extern Input invocation;
extern void *start_download(void *, void *, void *, int, int, void *);
void invoke(Function *self) {
 volatile unsigned int *d=self->data;
 void **context=*(void ***)((char*)thread_self()-0x78);
 d[1]=thread_id();d[2]=(context!=0&&context[0]!=0&&context[1]!=0);
 if(d[2] && *(void**)((char*)invocation.kernel+0x68)!=invocation.account) { invocation.phase=3; }
 else if(d[2]) { invocation.phase=1;start_download(invocation.future,invocation.manager,invocation.core,2,200,invocation.options);invocation.phase=2; }
 d[0]=1;
}
void *target(Function *self, void *info) { return 0; }
/* Opaque C callable: no target<T>; a valid native void RTTI reference. */
void *target_type(Function *self) { return (void*)void_type; }
void *callback_vtable[9]={destroy_inline,destroy_heap,clone_heap,clone_inline,destroy_inline,destroy_heap,invoke,target,target_type};
