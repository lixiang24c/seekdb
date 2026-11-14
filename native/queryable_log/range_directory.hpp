#pragma once

// Rebuildable disk AVL directory. Payload and transaction authority stay in
// queryable.log; this index contains only extent ranges and ordinal positions.
// A fixed direct-mapped node cache bounds extra memory regardless of extent count.
class RangeDirectory {
    struct Node {
        int64_t minimum=0,maximum=0,upper=0;
        uint64_t left=0,right=0,ordinal=0;
        int64_t height=1;
    };
    struct Slot {uint64_t id=0;Node node;};
    std::array<Slot,2048> cache{};
    std::filesystem::path path;
    cppdb::Descriptor file;
    uint64_t root=0,count=0;
    Node get(uint64_t id){
        auto &slot=cache[id%cache.size()];
        if(slot.id!=id){cppdb::read_at(file.fd,&slot.node,sizeof(Node),(id-1)*sizeof(Node));slot.id=id;}
        return slot.node;
    }
    void put(uint64_t id,const Node &node){
        const auto *p=reinterpret_cast<const unsigned char*>(&node);size_t left=sizeof(Node);uint64_t at=(id-1)*sizeof(Node);
        while(left){auto n=::pwrite(file.fd,p,left,at);if(n<0&&errno==EINTR)continue;if(n<=0)::error("write range directory");p+=n;left-=n;at+=n;}
        cache[id%cache.size()]={id,node};
    }
    int64_t height(uint64_t id){return id?get(id).height:0;}
    void refresh(Node &n){
        n.height=1+std::max(height(n.left),height(n.right));n.upper=n.maximum;
        if(n.left)n.upper=std::max(n.upper,get(n.left).upper);
        if(n.right)n.upper=std::max(n.upper,get(n.right).upper);
    }
    uint64_t rotate_right(uint64_t id){
        auto n=get(id);auto child=get(n.left);uint64_t result=n.left;n.left=child.right;child.right=id;
        refresh(n);put(id,n);refresh(child);put(result,child);return result;
    }
    uint64_t rotate_left(uint64_t id){
        auto n=get(id);auto child=get(n.right);uint64_t result=n.right;n.right=child.left;child.left=id;
        refresh(n);put(id,n);refresh(child);put(result,child);return result;
    }
    uint64_t insert(uint64_t id,const Node &value){
        if(!id){uint64_t next=++count;put(next,value);return next;}
        auto n=get(id);
        if(std::tie(value.minimum,value.ordinal)<std::tie(n.minimum,n.ordinal))n.left=insert(n.left,value);
        else n.right=insert(n.right,value);
        refresh(n);put(id,n);auto balance=height(n.left)-height(n.right);
        if(balance>1){auto child=get(n.left);if(std::tie(value.minimum,value.ordinal)>std::tie(child.minimum,child.ordinal)){n.left=rotate_left(n.left);refresh(n);put(id,n);}return rotate_right(id);}
        if(balance< -1){auto child=get(n.right);if(std::tie(value.minimum,value.ordinal)<std::tie(child.minimum,child.ordinal)){n.right=rotate_right(n.right);refresh(n);put(id,n);}return rotate_left(id);}
        return id;
    }
    template<class Visitor>bool search(uint64_t id,int64_t key,Visitor &visit){
        if(!id)return false;auto n=get(id);if(n.upper<key)return false;
        if(search(n.left,key,visit))return true;
        if(n.minimum>key)return false;
        if(key<=n.maximum&&visit(size_t(n.ordinal)))return true;
        return search(n.right,key,visit);
    }
public:
    template<class Extents>RangeDirectory(const std::filesystem::path &directory,const Extents &extents):
        path(directory/("query-result-range-"+std::to_string(::getpid()))),file(::open(path.c_str(),O_RDWR|O_CREAT|O_EXCL,0600)){
        if(file.fd<0)::error("open range directory");
        try{for(size_t i=0;i<extents.size();i++){auto &e=extents[i];Node n;n.minimum=e.minimum;n.maximum=e.maximum;n.upper=e.maximum;n.ordinal=i;root=insert(root,n);}}
        catch(...){::unlink(path.c_str());throw;}
    }
    ~RangeDirectory(){::unlink(path.c_str());}
    template<class Visitor>void find(int64_t key,Visitor visit){search(root,key,visit);}
};
