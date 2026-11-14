#pragma once
// Transaction snapshots share immutable pages. A write detaches one page;
// readers never detach, including range-for over a mutable catalog.
template<class T,size_t Width=64>class PagedDirectory {
    struct Page {std::vector<T> entries;mutable size_t weighted=SIZE_MAX;mutable uint64_t weight=0;};
    std::vector<std::shared_ptr<Page>> pages;size_t count=0;
public:
    size_t size()const{return count;}
    bool empty()const{return !count;}
    const T &operator[](size_t i)const{if(i>=count)throw std::out_of_range("directory ordinal");return pages[i/Width]->entries[i%Width];}
    const T &front()const{return (*this)[0];}
    T &edit(size_t i){
        if(i>=count)throw std::out_of_range("directory edit ordinal");auto &page=pages[i/Width];
        if(!page.unique())page=std::make_shared<Page>(*page);page->weighted=SIZE_MAX;return page->entries[i%Width];
    }
    void push_back(T value){
        size_t page=count/Width,slot=count%Width;
        if(!slot)pages.push_back(std::make_shared<Page>());
        else if(!pages[page].unique())pages[page]=std::make_shared<Page>(*pages[page]);
        auto &entries=pages[page]->entries;
        if(entries.size()>slot)entries.resize(slot);
        if(slot>=Width/2&&entries.capacity()<Width)entries.reserve(Width);
        entries.push_back(std::move(value));pages[page]->weighted=SIZE_MAX;++count;
    }
    void resize(size_t n){if(n>count)throw std::runtime_error("directory can only truncate");pages.resize(n/Width+(n%Width!=0));count=n;}
    uint64_t weight()const{
        uint64_t total=0;
        for(size_t i=0;i<pages.size();i++){
            auto &page=*pages[i];size_t used=std::min(Width,count-i*Width);
            if(page.weighted!=used){page.weight=0;for(size_t j=0;j<used;j++){auto value=page.entries[j].weight();if(value>UINT64_MAX-page.weight){page.weight=UINT64_MAX;break;}page.weight+=value;}page.weighted=used;}
            if(page.weight>UINT64_MAX-total)return UINT64_MAX;total+=page.weight;
        }return total;
    }
    static constexpr size_t page_width=Width;
    bool shares_page(const PagedDirectory &other,size_t ordinal)const{return ordinal<count&&ordinal<other.count&&pages[ordinal/Width]==other.pages[ordinal/Width];}
    size_t common_prefix(const PagedDirectory &other)const{
        size_t limit=std::min(count,other.count),i=0;
        while(i<limit){size_t stop=std::min(limit,(i/Width+1)*Width);
            if(pages[i/Width]==other.pages[i/Width]){i=stop;continue;}
            while(i<stop){if(!((*this)[i]==other[i]))return i;++i;}
        }return i;
    }
    class Iterator {
        const PagedDirectory *owner;size_t ordinal;
    public:
        Iterator(const PagedDirectory *p,size_t n):owner(p),ordinal(n){}
        const T &operator*()const{return (*owner)[ordinal];}
        Iterator &operator++(){++ordinal;return *this;}
        bool operator!=(const Iterator &other)const{return owner!=other.owner||ordinal!=other.ordinal;}
    };
    Iterator begin()const{return {this,0};}
    Iterator end()const{return {this,count};}
};
